from __future__ import annotations

import asyncio
import os
import posixpath
import tempfile
from typing import TYPE_CHECKING

from streamflow.core import utils
from streamflow.core.scheduling import JobStatus
from streamflow.core.workflow import Task, Job, Token, TerminationToken
from streamflow.data import remotepath
from streamflow.log_handler import logger
from streamflow.workflow.exception import WorkflowExecutionException

if TYPE_CHECKING:
    from streamflow.core.deployment import Connector
    from streamflow.core.workflow import OutputPort
    from typing import Optional, Any, List
    from typing_extensions import Text


async def _retrieve_output(
        job: Job,
        output_port: OutputPort,
        result: Any,
        status: JobStatus) -> None:
    token = await output_port.token_processor.compute_token(job, result, status)
    output_port.put(token)


class BaseTask(Task):

    async def _init_dir(self, job: Job) -> Text:
        if self.target is not None:
            path_processor = posixpath
            tempdir = '/tmp'
        else:
            path_processor = os.path
            tempdir = tempfile.gettempdir()
        dir_path = path_processor.join(tempdir, 'streamflow', utils.random_name())
        await remotepath.mkdir(self.get_connector(), job.get_resource(), dir_path)
        return dir_path

    async def _run_job(self, inputs: List[Token]) -> None:
        # Create job
        job = Job(
            name=posixpath.join(self.name, asyncio.current_task().get_name()),
            task=self,
            inputs=inputs)
        logger.info("Job {name} created".format(name=job.name))
        # Evaluate condition
        if self.condition is None or self.condition.evaluate():
            # Setup runtime environment
            if self.target is not None:
                await self.context.deployment_manager.deploy(self.target.model)
                await self.context.scheduler.schedule(job)
            # Initialize directories
            input_directory_task = asyncio.create_task(self._init_dir(job))
            output_directory_task = asyncio.create_task(self._init_dir(job))
            await asyncio.gather(input_directory_task, output_directory_task)
            job.input_directory = input_directory_task.result()
            job.output_directory = output_directory_task.result()
            # Update tokens after target assignment
            update_tasks = []
            for token in inputs:
                token_processor = self.input_ports[token.name].token_processor
                update_tasks.append(asyncio.create_task(token_processor.update_token(job, token)))
            job.inputs = await asyncio.gather(*update_tasks)
            # Execute task
            if self.target is not None:
                await self.context.scheduler.notify_status(job.name, JobStatus.RUNNING)
            result, status = await self.command.execute(job)
            # Notify completion to scheduler
            if self.target is not None:
                await self.context.scheduler.notify_status(job.name, status)
        else:
            # Execution skipped
            result = None
            status = JobStatus.SKIPPED
        # Retrieve output tokens
        output_tasks = []
        for output_port in self.output_ports.values():
            output_tasks.append(asyncio.create_task(_retrieve_output(job, output_port, result, status)))
        await asyncio.gather(*output_tasks)

    def get_connector(self) -> Optional[Connector]:
        if self.target is not None:
            return self.context.deployment_manager.get_connector(self.target.model.name)
        else:
            return None

    async def run(self) -> None:
        jobs = []
        # If there are input ports create jobs until termination token are received
        if self.input_ports:
            if self.input_combinator is None:
                raise WorkflowExecutionException("No InputCombinator specified for task {task}".format(task=self.name))
            while True:
                # Retrieve input tokens
                inputs = await self.input_combinator.get()
                # Check for termination
                if utils.check_termination(inputs):
                    break
                # Run job
                jobs.append(asyncio.create_task(
                    self._run_job(inputs),
                    name=utils.random_name()))
        # Otherwise simply run job
        else:
            jobs.append(asyncio.create_task(
                self._run_job([]),
                name=utils.random_name()))
        # Wait for jobs termination
        await asyncio.gather(*jobs)
        # Add a TerminationToken to each output port
        for port in self.output_ports.values():
            port.put(TerminationToken(name=port.name))
        logger.info("Task {name} completed".format(name=self.name))