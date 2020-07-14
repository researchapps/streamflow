import asyncio
import itertools
from asyncio import Queue, Task, FIRST_COMPLETED
from typing import List, MutableMapping, Text, Any, cast

from streamflow.core import utils
from streamflow.core.workflow import InputCombinator, Token, TerminationToken


def _flatten_list(hierarchical_list):
    if not hierarchical_list:
        return hierarchical_list
    if isinstance(hierarchical_list[0], list):
        return _flatten_list(hierarchical_list[0]) + _flatten_list(hierarchical_list[1:])
    return hierarchical_list[:1] + _flatten_list(hierarchical_list[1:])


class DotProductInputCombinator(InputCombinator):

    async def get(self) -> List[Token]:
        while True:
            # Retrieve input tokens
            get_tasks = []
            for port in self.ports.values():
                get_tasks.append(asyncio.create_task(port.get()))
            inputs = await asyncio.gather(*get_tasks)
            # Check for termination
            if utils.check_termination(inputs):
                break
            # Return input tokens
            return _flatten_list(inputs)
        # When terminated, return a TerminationToken
        return [TerminationToken(self.name)]


class CartesianProductInputCombinator(InputCombinator):

    def __init__(self, name: Text):
        super().__init__(name)
        self.queue: Queue = Queue()
        self.terminated: List[Text] = []
        self.token_lists: MutableMapping[Text, List[Any]] = {}

    async def _cartesian_multiplier(self):
        input_tasks = []
        for port_name, port in self.ports.items():
            input_tasks.append(asyncio.create_task(port.get(), name=port_name))
        while True:
            finished, unfinished = await asyncio.wait(input_tasks, return_when=FIRST_COMPLETED)
            input_tasks = list(unfinished)
            for task in finished:
                task_name = cast(Task, task).get_name()
                token = task.result()
                # If a TerminationToken is received, the corresponding port terminated its outputs
                if isinstance(token, TerminationToken) or (isinstance(token, List) and utils.check_termination(token)):
                    self.terminated.append(task_name)
                    # When the last port terminates, the entire combinator terminates
                    if len(self.terminated) == len(self.ports):
                        self.queue.put_nowait([TerminationToken(self.name)])
                        return
                else:
                    # Get all combinations of the new element with the others
                    list_of_lists = []
                    for name, token_list in self.token_lists.items():
                        if name == task_name:
                            list_of_lists.append([token])
                        else:
                            list_of_lists.append(token_list)
                    cartesian_product = list(itertools.product(*list_of_lists))
                    # Put all combinations in the queue
                    for element in cartesian_product:
                        self.queue.put_nowait(list(element))
                    # Create a new task in place of the completed one
                    input_tasks.append(asyncio.create_task(self.ports[task_name].get(), name=task_name))

    async def _initialize(self):
        # Initialize token lists
        for port in self.ports.values():
            self.token_lists[port.name] = []
        # Retrieve initial input tokens
        input_tasks = []
        for port in self.ports.values():
            input_tasks.append(asyncio.create_task(port.get()))
        inputs = {k: v for (k, v) in zip(self.ports.keys(), await asyncio.gather(*input_tasks))}
        # Check for early termination and return a TerminationToken
        if utils.check_termination(inputs.values()):
            return [TerminationToken(self.name)]
        # Put initial inputs in token lists and in queue
        for name, token in inputs.items():
            self.token_lists[name].append(token)
        self.queue.put_nowait(list(inputs.values()))
        # Start cartesian mulitplier task
        asyncio.create_task(self._cartesian_multiplier())

    async def get(self) -> List[Token]:
        # If lists are empty it means that this is the first call to the get() function
        if not self.token_lists:
            await self._initialize()
        # Otherwise simply wait for new input tokens
        inputs = await self.queue.get()
        return _flatten_list(inputs)