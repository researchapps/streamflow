import asyncio
import base64
import io
import os
import posixpath
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from abc import ABC
from pathlib import Path
from typing import MutableMapping, List, Optional, Any, Tuple

import yaml
from kubernetes_asyncio import client
from kubernetes_asyncio.client import Configuration, ApiClient
from kubernetes_asyncio.config import incluster_config, ConfigException, load_kube_config
from kubernetes_asyncio.stream import WsApiClient, ws_client
from typing_extensions import Text

from streamflow.deployment.base import BaseConnector
from streamflow.core.scheduling import Resource
from streamflow.log_handler import logger

SERVICE_NAMESPACE_FILENAME = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


class PatchedInClusterConfigLoader(incluster_config.InClusterConfigLoader):

    def load_and_set(self, configuration: Optional[Configuration] = None):
        self._load_config()
        self._set_config(configuration)

    def _set_config(self, configuration: Optional[Configuration] = None):
        if configuration is None:
            super()._set_config()
        configuration.host = self.host
        configuration.ssl_ca_cert = self.ssl_ca_cert
        configuration.api_key['authorization'] = "bearer " + self.token


class BaseHelmConnector(BaseConnector, ABC):

    def __init__(self,
                 streamflow_config_dir: Text,
                 inCluster: Optional[bool] = False,
                 kubeconfig: Optional[Text] = os.path.join(os.environ['HOME'], ".kube", "config"),
                 namespace: Optional[Text] = None,
                 releaseName: Optional[Text] = "release-%s" % str(uuid.uuid1()),
                 transferBufferSize: Optional[int] = (32 << 20) - 1):
        super().__init__(streamflow_config_dir)
        self.inCluster = inCluster
        self.kubeconfig = kubeconfig
        self.namespace = namespace
        self.releaseName = releaseName
        self.transferBufferSize = transferBufferSize
        self.configuration: Optional[Configuration] = None
        self.client: Optional[client.CoreV1Api] = None
        self.client_ws: Optional[client.CoreV1Api] = None

    async def _build_helper_file(self,
                                 kube_client_ws: client.CoreV1Api,
                                 target: str,
                                 environment: MutableMapping[str, str] = None,
                                 workdir: str = None
                                 ) -> str:
        file_contents = "".join([
            '#!/bin/sh\n',
            '{environment}',
            '{workdir}',
            'sh -c "$(echo $@ | base64 --decode)"\n'
        ]).format(
            environment="".join(["export %s=\"%s\"\n" % (key, value) for (key, value) in
                                 environment.items()]) if environment is not None else "",
            workdir="cd {workdir}\n".format(workdir=workdir) if workdir is not None else ""
        )
        file_name = tempfile.mktemp()
        with open(file_name, mode='w') as file:
            file.write(file_contents)
        os.chmod(file_name, os.stat(file_name).st_mode | stat.S_IEXEC)
        parent_directory = str(Path(file_name).parent)
        pod, container = target.split(':')
        await kube_client_ws.connect_get_namespaced_pod_exec(
            name=pod,
            namespace=self.namespace or 'default',
            container=container,
            command=["mkdir", "-p", parent_directory],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False)
        await self._copy_local_to_remote(file_name, file_name, target)
        return file_name

    def _configure_incluster_namespace(self):
        if self.namespace is None:
            if not os.path.isfile(SERVICE_NAMESPACE_FILENAME):
                raise ConfigException(
                    "Service namespace file does not exists.")

            with open(SERVICE_NAMESPACE_FILENAME) as f:
                self.namespace = f.read()
                if not self.namespace:
                    raise ConfigException("Namespace file exists but empty.")

    async def _copy_remote_to_remote(self, src: Text, dst: Text, resource: Text, source_remote: Text) -> None:
        source_remote = source_remote or resource
        if source_remote == resource:
            if src != dst:
                command = ['/bin/cp', "-rf", src, dst]
                await self.run(resource, command)
                return
        else:
            temp_dir = tempfile.mkdtemp()
            await self._copy_remote_to_local(src, temp_dir, source_remote)
            copy_tasks = []
            for element in os.listdir(temp_dir):
                copy_tasks.append(asyncio.create_task(
                    self._copy_local_to_remote(os.path.join(temp_dir, element), dst, resource)))
            await asyncio.gather(*copy_tasks)
            shutil.rmtree(temp_dir)

    async def _copy_local_to_remote(self, src: Text, dst: Text, resource: Text):
        with tempfile.TemporaryFile() as tar_buffer:
            with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
                tar.add(src, arcname=dst)
            tar_buffer.seek(0)
            kube_client_ws = await self._get_client_ws()
            pod, container = resource.split(':')
            command = ['tar', 'xf', '-', '-C', '/']
            response = await kube_client_ws.connect_get_namespaced_pod_exec(
                name=pod,
                namespace=self.namespace or 'default',
                container=container,
                command=command,
                stderr=True,
                stdin=True,
                stdout=True,
                tty=False,
                _preload_content=False)
            while not response.closed:
                content = tar_buffer.read(self.transferBufferSize)
                if content:
                    channel_prefix = bytes(chr(ws_client.STDIN_CHANNEL), "ascii")
                    payload = channel_prefix + content
                    await response.send_bytes(payload)
                else:
                    break
                async for _ in response:
                    pass
            await response.close()

    async def _copy_remote_to_local(self, src: Text, dst: Text, resource: Text):
        kube_client_ws = await self._get_client_ws()
        pod, container = resource.split(':')
        command = ['tar', 'cPf', '-', src]
        response = await kube_client_ws.connect_get_namespaced_pod_exec(
            name=pod,
            namespace=self.namespace or 'default',
            container=container,
            command=command,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            _preload_content=False)
        with io.BytesIO() as byte_buffer:
            while not response.closed:
                async for msg in response:
                    channel = msg.data[0]
                    data = msg.data[1:]
                    if data and channel == ws_client.STDOUT_CHANNEL:
                        byte_buffer.write(data)
            await response.close()
            byte_buffer.flush()
            byte_buffer.seek(0)
            with tarfile.open(fileobj=byte_buffer, mode='r:') as tar:
                for member in tar.getmembers():
                    if os.path.isdir(dst):
                        if member.path == src:
                            member.path = posixpath.basename(member.path)
                        else:
                            member.path = posixpath.relpath(member.path, src)
                        tar.extract(member, dst)
                    elif member.isfile():
                        with tar.extractfile(member) as inputfile:
                            with open(dst, 'wb') as outputfile:
                                outputfile.write(inputfile.read())
                    else:
                        parent_dir = str(Path(dst).parent)
                        member.path = posixpath.relpath(member.path, src)
                        tar.extract(member, parent_dir)

    async def _get_client(self) -> client.CoreV1Api:
        if self.client is None:
            configuration = await self._get_configuration()
            self.client = client.CoreV1Api(api_client=ApiClient(configuration=configuration))
        return self.client

    async def _get_client_ws(self) -> client.CoreV1Api:
        if self.client_ws is None:
            configuration = await self._get_configuration()
            self.client_ws = client.CoreV1Api(api_client=WsApiClient(configuration=configuration))
        return self.client_ws

    async def _get_configuration(self) -> Configuration:
        if self.configuration is None:
            self.configuration = Configuration()
            if self.inCluster:
                loader = PatchedInClusterConfigLoader(token_filename=incluster_config.SERVICE_TOKEN_FILENAME,
                                                      cert_filename=incluster_config.SERVICE_CERT_FILENAME)
                loader.load_and_set(configuration=self.configuration)
                self._configure_incluster_namespace()
            else:
                await load_kube_config(config_file=self.kubeconfig, client_configuration=self.configuration)
        return self.configuration

    async def get_available_resources(self, service):
        kube_client = await self._get_client()
        pods = await kube_client.list_namespaced_pod(
            namespace=self.namespace or 'default',
            label_selector="app.kubernetes.io/instance={}".format(self.releaseName),
            field_selector="status.phase=Running"
        )
        valid_targets = {}
        for pod in pods.items:
            for container in pod.spec.containers:
                if service == container.name:
                    resource_name = pod.metadata.name + ':' + service
                    valid_targets[resource_name] = Resource(name=resource_name, hostname=pod.status.pod_ip)
                    break
        return valid_targets

    async def run(self,
                  resource: str,
                  command: List[str],
                  environment: MutableMapping[str, str] = None,
                  workdir: str = None,
                  capture_output: bool = False) -> Optional[Tuple[Optional[Any], int]]:
        kube_client_ws = await self._get_client_ws()
        helper_file_name = await self._build_helper_file(kube_client_ws, resource, environment, workdir)
        logger.debug("Executing {command}".format(command=command, resource=resource))
        command = [helper_file_name, base64.b64encode(" ".join(command).encode('utf-8')).decode('utf-8')]
        pod, container = resource.split(':')
        response = await kube_client_ws.connect_get_namespaced_pod_exec(
            name=pod,
            namespace=self.namespace or 'default',
            container=container,
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=not capture_output)
        if capture_output:
            with io.StringIO() as out_buffer, io.StringIO() as err_buffer:
                while not response.closed:
                    async for msg in response:
                        data = msg.data.decode('utf-8', 'replace')
                        channel = ord(data[0])
                        data = data[1:]
                        if data and channel in [ws_client.STDOUT_CHANNEL, ws_client.STDERR_CHANNEL]:
                            out_buffer.write(data)
                        elif data and channel == ws_client.ERROR_CHANNEL:
                            err_buffer.write(data)
                err = yaml.safe_load(err_buffer.getvalue())
                if err['status'] == "Success":
                    return out_buffer.getvalue(), 0
                else:
                    return out_buffer.getvalue(), int(err['details']['causes'][0]['message'])


class Helm2Connector(BaseHelmConnector):

    def __init__(self,
                 streamflow_config_dir: Text,
                 chart: Text,
                 debug: Optional[bool] = False,
                 home: Optional[Text] = os.path.join(os.environ['HOME'], ".helm"),
                 kubeContext: Optional[Text] = None,
                 kubeconfig: Optional[Text] = None,
                 tillerConnectionTimeout: Optional[int] = None,
                 tillerNamespace: Optional[Text] = None,
                 atomic: Optional[bool] = False,
                 caFile: Optional[Text] = None,
                 certFile: Optional[Text] = None,
                 depUp: Optional[bool] = False,
                 description: Optional[Text] = None,
                 devel: Optional[bool] = False,
                 inCluster: Optional[bool] = False,
                 init: Optional[bool] = False,
                 keyFile: Optional[Text] = None,
                 keyring: Optional[Text] = None,
                 releaseName: Optional[Text] = None,
                 nameTemplate: Optional[Text] = None,
                 namespace: Optional[Text] = None,
                 noCrdHook: Optional[bool] = False,
                 noHooks: Optional[bool] = False,
                 password: Optional[Text] = None,
                 renderSubchartNotes: Optional[bool] = False,
                 repo: Optional[Text] = None,
                 commandLineValues: Optional[List[Text]] = None,
                 fileValues: Optional[List[Text]] = None,
                 stringValues: Optional[List[Text]] = None,
                 timeout: Optional[int] = str(60000),
                 tls: Optional[bool] = False,
                 tlscacert: Optional[Text] = None,
                 tlscert: Optional[Text] = None,
                 tlshostname: Optional[Text] = None,
                 tlskey: Optional[Text] = None,
                 tlsverify: Optional[bool] = False,
                 username: Optional[Text] = None,
                 yamlValues: Optional[List[Text]] = None,
                 verify: Optional[bool] = False,
                 chartVersion: Optional[Text] = None,
                 wait: Optional[bool] = True,
                 purge: Optional[bool] = True,
                 transferBufferSize: Optional[int] = None
                 ):
        super().__init__(
            streamflow_config_dir=streamflow_config_dir,
            inCluster=inCluster,
            kubeconfig=kubeconfig,
            namespace=namespace,
            releaseName=releaseName,
            transferBufferSize=transferBufferSize
        )
        self.chart = os.path.join(streamflow_config_dir, chart)
        self.debug = debug
        self.home = home
        self.kubeContext = kubeContext
        self.tillerConnectionTimeout = tillerConnectionTimeout
        self.tillerNamespace = tillerNamespace
        self.atomic = atomic
        self.caFile = caFile
        self.certFile = certFile
        self.depUp = depUp
        self.description = description
        self.devel = devel
        self.keyFile = keyFile
        self.keyring = keyring
        self.nameTemplate = nameTemplate
        self.noCrdHook = noCrdHook
        self.noHooks = noHooks
        self.password = password
        self.renderSubchartNotes = renderSubchartNotes
        self.repo = repo
        self.commandLineValues = commandLineValues
        self.fileValues = fileValues
        self.stringValues = stringValues
        self.tlshostname = tlshostname
        self.username = username
        self.yamlValues = yamlValues
        self.verify = verify
        self.chartVersion = chartVersion
        self.wait = wait
        self.purge = purge
        self.timeout = timeout
        self.tls = tls
        self.tlscacert = tlscacert
        self.tlscert = tlscert
        self.tlskey = tlskey
        self.tlsverify = tlsverify
        if init:
            self._init_helm()

    def _init_helm(self):
        init_command = self.base_command() + "".join([
            "init "
            "--upgrade "
            "{wait}"
        ]).format(
            wait=self.get_option("wait", self.wait)
        )
        logger.debug("Executing {command}".format(command=init_command))
        return subprocess.run(shlex.split(init_command))

    def base_command(self):
        return (
            "helm "
            "{debug}"
            "{home}"
            "{kubeContext}"
            "{kubeconfig}"
            "{tillerConnectionTimeout}"
            "{tillerNamespace}"
        ).format(
            debug=self.get_option("debug", self.debug),
            home=self.get_option("home", self.home),
            kubeContext=self.get_option("kube-context", self.kubeContext),
            kubeconfig=self.get_option("kubeconfig", self.kubeconfig),
            tillerConnectionTimeout=self.get_option("tiller-connection-timeout", self.tillerConnectionTimeout),
            tillerNamespace=self.get_option("tiller-namespace", self.tillerNamespace)
        )

    async def deploy(self) -> None:
        deploy_command = self.base_command() + "".join([
            "install "
            "{atomic}"
            "{caFile}"
            "{certFile}"
            "{depUp}"
            "{description}"
            "{devel}"
            "{keyFile}"
            "{keyring}"
            "{releaseName}"
            "{nameTemplate}"
            "{namespace}"
            "{noCrdHook}"
            "{noHooks}"
            "{password}"
            "{renderSubchartNotes}"
            "{repo}"
            "{commandLineValues}"
            "{fileValues}"
            "{stringValues}"
            "{timeout}"
            "{tls}"
            "{tlscacert}"
            "{tlscert}"
            "{tlshostname}"
            "{tlskey}"
            "{tlsverify}"
            "{username}"
            "{yamlValues}"
            "{verify}"
            "{chartVersion}"
            "{wait}"
            "{chart}"
        ]).format(
            atomic=self.get_option("atomic", self.atomic),
            caFile=self.get_option("ca-file", self.caFile),
            certFile=self.get_option("cert-file", self.certFile),
            depUp=self.get_option("dep-up", self.depUp),
            description=self.get_option("description", self.description),
            devel=self.get_option("devel", self.devel),
            keyFile=self.get_option("key-file", self.keyFile),
            keyring=self.get_option("keyring", self.keyring),
            releaseName=self.get_option("name", self.releaseName),
            nameTemplate=self.get_option("name-template", self.nameTemplate),
            namespace=self.get_option("namespace", self.namespace),
            noCrdHook=self.get_option("no-crd-hook", self.noCrdHook),
            noHooks=self.get_option("no-hooks", self.noHooks),
            password=self.get_option("password", self.password),
            renderSubchartNotes=self.get_option("render-subchart-notes", self.renderSubchartNotes),
            repo=self.get_option("repo", self.repo),
            commandLineValues=self.get_option("set", self.commandLineValues),
            fileValues=self.get_option("set-file", self.fileValues),
            stringValues=self.get_option("set-string", self.stringValues),
            timeout=self.get_option("timeout", self.timeout),
            tls=self.get_option("tls", self.tls),
            tlscacert=self.get_option("tls-ca-cert", self.tlscacert),
            tlscert=self.get_option("tls-cert", self.tlscert),
            tlshostname=self.get_option("tls-hostname", self.tlshostname),
            tlskey=self.get_option("tls-key", self.tlskey),
            tlsverify=self.get_option("tls-verify", self.tlsverify),
            username=self.get_option("username", self.username),
            yamlValues=self.get_option("values", self.yamlValues),
            verify=self.get_option("verify", self.verify),
            chartVersion=self.get_option("version", self.chartVersion),
            wait=self.get_option("wait", self.wait),
            chart="\"{chart}\"".format(chart=self.chart)
        )
        logger.debug("Executing {command}".format(command=deploy_command))
        proc = await asyncio.create_subprocess_exec(*shlex.split(deploy_command))
        await proc.wait()

    async def undeploy(self) -> None:
        undeploy_command = self.base_command() + (
            "delete "
            "{description}"
            "{noHooks}"
            "{purge}"
            "{timeout}"
            "{tls}"
            "{tlscacert}"
            "{tlscert}"
            "{tlshostname}"
            "{tlskey}"
            "{tlsverify}"
            "{releaseName}"
        ).format(
            description=self.get_option("description", self.description),
            noHooks=self.get_option("no-hooks", self.noHooks),
            timeout=self.get_option("timeout", self.timeout),
            purge=self.get_option("purge", self.purge),
            tls=self.get_option("tls", self.tls),
            tlscacert=self.get_option("tls-ca-cert", self.tlscacert),
            tlscert=self.get_option("tls-cert", self.tlscert),
            tlshostname=self.get_option("tls-hostname", self.tlshostname),
            tlskey=self.get_option("tls-key", self.tlskey),
            tlsverify=self.get_option("tls-verify", self.tlsverify),
            releaseName=self.releaseName
        )
        logger.debug("Executing {command}".format(command=undeploy_command))
        proc = await asyncio.create_subprocess_exec(*shlex.split(undeploy_command))
        await proc.wait()


class Helm3Connector(BaseHelmConnector):
    def __init__(self,
                 streamflow_config_dir: Text,
                 chart: Text,
                 debug: Optional[bool] = False,
                 kubeContext: Optional[Text] = None,
                 kubeconfig: Optional[Text] = None,
                 atomic: Optional[bool] = False,
                 caFile: Optional[Text] = None,
                 certFile: Optional[Text] = None,
                 depUp: Optional[bool] = False,
                 devel: Optional[bool] = False,
                 inCluster: Optional[bool] = False,
                 keepHistory: Optional[bool] = False,
                 keyFile: Optional[Text] = None,
                 keyring: Optional[Text] = None,
                 releaseName: Optional[Text] = None,
                 nameTemplate: Optional[Text] = None,
                 namespace: Optional[Text] = None,
                 noHooks: Optional[bool] = False,
                 password: Optional[Text] = None,
                 renderSubchartNotes: Optional[bool] = False,
                 repo: Optional[Text] = None,
                 commandLineValues: Optional[List[Text]] = None,
                 fileValues: Optional[List[Text]] = None,
                 registryConfig: Optional[Text] = os.path.join(os.environ['HOME'], ".config/helm/registry.json"),
                 repositoryCache: Optional[Text] = os.path.join(os.environ['HOME'], ".cache/helm/repository"),
                 repositoryConfig: Optional[Text] = os.path.join(os.environ['HOME'], ".config/helm/repositories.yaml"),
                 stringValues: Optional[List[Text]] = None,
                 skipCrds: Optional[bool] = False,
                 timeout: Optional[Text] = "1000m",
                 username: Optional[Text] = None,
                 yamlValues: Optional[List[Text]] = None,
                 verify: Optional[bool] = False,
                 chartVersion: Optional[Text] = None,
                 wait: Optional[bool] = True,
                 transferBufferSize: Optional[int] = None
                 ):
        super().__init__(
            streamflow_config_dir=streamflow_config_dir,
            inCluster=inCluster,
            kubeconfig=kubeconfig,
            namespace=namespace,
            releaseName=releaseName,
            transferBufferSize=transferBufferSize
        )
        self.chart = os.path.join(streamflow_config_dir, chart)
        self.debug = debug
        self.kubeContext = kubeContext
        self.atomic = atomic
        self.caFile = caFile
        self.certFile = certFile
        self.depUp = depUp
        self.devel = devel
        self.keepHistory = keepHistory
        self.keyFile = keyFile
        self.keyring = keyring
        self.nameTemplate = nameTemplate
        self.noHooks = noHooks
        self.password = password
        self.renderSubchartNotes = renderSubchartNotes
        self.repo = repo
        self.commandLineValues = commandLineValues
        self.fileValues = fileValues
        self.stringValues = stringValues
        self.skipCrds = skipCrds
        self.registryConfig = registryConfig
        self.repositoryCache = repositoryCache
        self.repositoryConfig = repositoryConfig
        self.username = username
        self.yamlValues = yamlValues
        self.verify = verify
        self.chartVersion = chartVersion
        self.wait = wait
        self.timeout = timeout

    def base_command(self):
        return (
            "helm "
            "{debug}"
            "{kubeContext}"
            "{kubeconfig}"
            "{namespace}"
            "{registryConfig}"
            "{repositoryCache}"
            "{repositoryConfig}"
        ).format(
            debug=self.get_option("debug", self.debug),
            kubeContext=self.get_option("kube-context", self.kubeContext),
            kubeconfig=self.get_option("kubeconfig", self.kubeconfig),
            namespace=self.get_option("namespace", self.namespace),
            registryConfig=self.get_option("registry-config", self.registryConfig),
            repositoryCache=self.get_option("repository-cache", self.repositoryCache),
            repositoryConfig=self.get_option("repository-config", self.repositoryConfig),
        )

    async def deploy(self) -> None:
        deploy_command = self.base_command() + "".join([
            "install "
            "{atomic}"
            "{caFile}"
            "{certFile}"
            "{depUp}"
            "{devel}"
            "{keyFile}"
            "{keyring}"
            "{nameTemplate}"
            "{noHooks}"
            "{password}"
            "{renderSubchartNotes}"
            "{repo}"
            "{commandLineValues}"
            "{fileValues}"
            "{stringValues}"
            "{skipCrds}"
            "{timeout}"
            "{username}"
            "{yamlValues}"
            "{verify}"
            "{chartVersion}"
            "{wait}"
            "{releaseName}"
            "{chart}"
        ]).format(
            atomic=self.get_option("atomic", self.atomic),
            caFile=self.get_option("ca-file", self.caFile),
            certFile=self.get_option("cert-file", self.certFile),
            depUp=self.get_option("dep-up", self.depUp),
            devel=self.get_option("devel", self.devel),
            keyFile=self.get_option("key-file", self.keyFile),
            keyring=self.get_option("keyring", self.keyring),
            nameTemplate=self.get_option("name-template", self.nameTemplate),
            namespace=self.get_option("namespace", self.namespace),
            noHooks=self.get_option("no-hooks", self.noHooks),
            password=self.get_option("password", self.password),
            renderSubchartNotes=self.get_option("render-subchart-notes", self.renderSubchartNotes),
            repo=self.get_option("repo", self.repo),
            commandLineValues=self.get_option("set", self.commandLineValues),
            fileValues=self.get_option("set-file", self.fileValues),
            stringValues=self.get_option("set-string", self.stringValues),
            skipCrds=self.get_option("skip-crds", self.skipCrds),
            timeout=self.get_option("timeout", self.timeout),
            username=self.get_option("username", self.username),
            yamlValues=self.get_option("values", self.yamlValues),
            verify=self.get_option("verify", self.verify),
            chartVersion=self.get_option("version", self.chartVersion),
            wait=self.get_option("wait", self.wait),
            releaseName="{releaseName} ".format(releaseName=self.releaseName),
            chart="\"{chart}\"".format(chart=self.chart)
        )
        logger.debug("Executing {command}".format(command=deploy_command))
        proc = await asyncio.create_subprocess_exec(*shlex.split(deploy_command))
        await proc.wait()

    async def undeploy(self) -> None:
        # Undeploy model
        undeploy_command = self.base_command() + (
            "uninstall "
            "{keepHistory}"
            "{noHooks}"
            "{timeout}"
            "{releaseName}"
        ).format(
            keepHistory=self.get_option("keep-history", self.keepHistory),
            noHooks=self.get_option("no-hooks", self.noHooks),
            timeout=self.get_option("timeout", self.timeout),
            releaseName=self.releaseName
        )
        logger.debug("Executing {command}".format(command=undeploy_command))
        proc = await asyncio.create_subprocess_exec(*shlex.split(undeploy_command))
        await proc.wait()
        # Close connections
        if self.client is not None:
            await self.client.api_client.close()
            self.client = None
        if self.client_ws is not None:
            await self.client_ws.api_client.close()
            self.client_ws = None
        self.configuration = None