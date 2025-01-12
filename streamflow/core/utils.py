from __future__ import annotations

import asyncio
import base64
import datetime
import importlib
import itertools
import os
import posixpath
import shlex
import uuid
from typing import (
    Any,
    MutableMapping,
    MutableSequence,
    TYPE_CHECKING,
)

from jsonref import loads

from streamflow.core.exception import WorkflowExecutionException

if TYPE_CHECKING:
    from streamflow.core.context import SchemaEntity
    from streamflow.core.deployment import Connector, Location
    from streamflow.core.workflow import Token
    from typing import Iterable


class NamesStack:
    def __init__(self):
        self.stack: MutableSequence[set] = [set()]

    def add_scope(self):
        self.stack.append(set())

    def add_name(self, name: str):
        self.stack[-1].add(name)

    def delete_scope(self):
        self.stack.pop()

    def delete_name(self, name: str):
        self.stack[-1].remove(name)

    def global_names(self) -> set[str]:
        names = self.stack[0].copy()
        if len(self.stack) > 1:
            for scope in self.stack[1:]:
                names = names.difference(scope)
        return names

    def __contains__(self, name: str) -> bool:
        for scope in self.stack:
            if name in scope:
                return True
        return False


def create_command(
    command: MutableSequence[str],
    environment: MutableMapping[str, str] = None,
    workdir: str | None = None,
    stdin: int | str | None = None,
    stdout: int | str = asyncio.subprocess.STDOUT,
    stderr: int | str = asyncio.subprocess.STDOUT,
) -> str:
    command = "".join(
        "{workdir}" "{environment}" "{command}" "{stdin}" "{stdout}" "{stderr}"
    ).format(
        workdir=f"cd {workdir} && " if workdir is not None else "",
        environment="".join(
            [f'export {key}="{value}" && ' for (key, value) in environment.items()]
        )
        if environment is not None
        else "",
        command=" ".join(command),
        stdin=f" < {shlex.quote(stdin)}" if stdin is not None else "",
        stdout=f" > {shlex.quote(stdout)}"
        if stdout != asyncio.subprocess.STDOUT
        else "",
        stderr=(
            " 2>&1"
            if stderr == stdout
            else f" 2>{shlex.quote(stderr)}"
            if stderr != asyncio.subprocess.STDOUT
            else ""
        ),
    )
    return command


def dict_product(**kwargs) -> MutableMapping[Any, Any]:
    keys = kwargs.keys()
    vals = kwargs.values()
    for instance in itertools.product(*vals):
        yield dict(zip(keys, list(instance)))


def encode_command(command: str, shell: str = "sh"):
    return "echo {command} | base64 -d | {shell}".format(
        command=base64.b64encode(command.encode("utf-8")).decode("utf-8"),
        shell=shell,  # nosec
    )


def flatten_list(hierarchical_list):
    if not hierarchical_list:
        return hierarchical_list
    flat_list = []
    for el in hierarchical_list:
        if isinstance(el, MutableSequence):
            flat_list.extend(flatten_list(el))
        else:
            flat_list.append(el)
    return flat_list


def format_seconds_to_hhmmss(seconds: int) -> str:
    hours = seconds // (60 * 60)
    seconds %= 60 * 60
    minutes = seconds // 60
    seconds %= 60
    return "%02i:%02i:%02i" % (hours, minutes, seconds)


def get_class_fullname(cls: type):
    return cls.__module__ + "." + cls.__qualname__


def get_class_from_name(name: str) -> type:
    module_name, class_name = name.rsplit(".", 1)
    return getattr(importlib.import_module(module_name), class_name)


def get_date_from_ns(timestamp: int) -> str:
    base = datetime.datetime(1970, 1, 1)
    delta = datetime.timedelta(microseconds=round(timestamp / 1000))
    return (base + delta).replace(tzinfo=datetime.timezone.utc).isoformat()


async def get_remote_to_remote_write_command(
    src_connector: Connector,
    src_location: Location,
    src: str,
    dst_connector: Connector,
    dst_locations: MutableSequence[Location],
    dst: str,
) -> MutableSequence[str]:
    if posixpath.basename(src) != posixpath.basename(dst):
        result, status = await src_connector.run(
            location=src_location,
            command=[f'test -d "{src}"'],
            capture_output=True,
        )
        if status > 1:
            raise WorkflowExecutionException(result)
        # If is a directory
        elif status == 0:
            await asyncio.gather(
                *(
                    asyncio.create_task(
                        dst_connector.run(
                            location=dst_location, command=["mkdir", "-p", dst]
                        )
                    )
                    for dst_location in dst_locations
                )
            )
            return ["tar", "xf", "-", "-C", dst, "--strip-components", "1"]
        # If is a file
        else:
            return ["tar", "xf", "-", "-O", ">", dst]
    else:
        return ["tar", "xf", "-", "-C", posixpath.dirname(dst)]


def get_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    else:
        total_size = 0
        for dirpath, _, filenames in os.walk(path, followlinks=True):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                total_size += os.path.getsize(fp)
        return total_size


def get_tag(tokens: Iterable[Token]) -> str:
    output_tag = "0"
    for tag in [t.tag for t in tokens]:
        if len(tag) > len(output_tag):
            output_tag = tag
    return output_tag


def inject_schema(
    schema: MutableMapping[str, Any],
    classes: MutableMapping[str, type[SchemaEntity]],
    definition_name: str,
):
    for name, entity in classes.items():
        if entity_schema := entity.get_schema():
            with open(entity_schema) as f:
                entity_schema = loads(
                    f.read(),
                    base_uri=f"file://{os.path.dirname(entity_schema)}/",
                    jsonschema=True,
                )
            schema["definitions"][definition_name]["properties"]["type"].setdefault(
                "enum", []
            ).append(name)
            schema["definitions"][definition_name]["definitions"][name] = entity_schema
            schema["definitions"][definition_name].setdefault("allOf", []).append(
                {
                    "if": {"properties": {"type": {"const": name}}},
                    "then": {"properties": {"config": entity_schema}},
                }
            )


def random_name() -> str:
    return str(uuid.uuid4())


def wrap_command(command: str):
    return ["/bin/sh", "-c", f"{command}"]
