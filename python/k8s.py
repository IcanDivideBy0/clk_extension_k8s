#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import grp
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from shlex import split

import click
import yaml
from clk.config import config
from clk.decorators import (argument, flag, group, option, param_config,
                            table_fields, table_format)
from clk.lib import (TablePrinter, call, cd, check_output, copy, deepcopy,
                     download, extract, get_keyring, is_port_available,
                     makedirs, move, read, rm, tempdir, temporary_file,
                     updated_env, which)
from clk.log import get_logger

LOGGER = get_logger(__name__)


class KubeCtl:
    def __init__(self):
        self._context = None

    @property
    def context(self):
        if self._context is not None:
            return self._context
        else:
            if config.k8s.distribution == "k3d":
                return "k3d-k3s-default"
            if config.k8s.distribution == "kind":
                return "kind-kind"
            else:
                return None

    @context.setter
    def context(self, value):
        self._context = value

    def call(self, arguments):
        context = self.context
        if context is not None:
            call(['kubectl', '--context', context] + arguments)
        else:
            call(['kubectl'] + arguments)

    def output(self, arguments):
        context = self.context
        if context is not None:
            return check_output(['kubectl', '--context', context] + arguments)
        else:
            return check_output(['kubectl'] + arguments)


@group()
@param_config('kubectl', '--context', '-c', typ=KubeCtl, help="The kubectl context to use")
@param_config('k8s', '--distribution', '-d', help="Distribution to use", default='kind',
              type=click.Choice(['k3d', 'kind']))  # yapf: disable
def k8s():
    """Manipulate k8s"""


bin_dir = Path('~/.local/bin').expanduser()
k3d_url = 'https://github.com/rancher/k3d/releases/download/v4.4.4/k3d-linux-amd64'
kind_url = 'https://kind.sigs.k8s.io/dl/v0.11.1/kind-linux-amd64'
helm_url = 'https://get.helm.sh/helm-v3.6.3-linux-amd64.tar.gz'
kubectl_url = 'https://dl.k8s.io/release/v1.21.2/bin/linux/amd64/kubectl'
kubectl_buildkit_url = \
    'https://github.com/vmware-tanzu/buildkit-cli-for-kubectl/releases/download/v0.1.3/linux-v0.1.3.tgz'
tilt_url = 'https://github.com/tilt-dev/tilt/releases/download/v0.22.7/tilt.0.22.7.linux.x86_64.tar.gz'
kind_config = """
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: kind
kubeadmConfigPatches:
- |
  apiVersion: kubeadm.k8s.io/v1beta2
  kind: ClusterConfiguration
  metadata:
    name: config
  apiServer:
    extraArgs:
      "feature-gates": "EphemeralContainers=true"
  scheduler:
    extraArgs:
      "feature-gates": "EphemeralContainers=true"
  controllerManager:
    extraArgs:
      "feature-gates": "EphemeralContainers=true"
- |
  apiVersion: kubeadm.k8s.io/v1beta2
  kind: InitConfiguration
  metadata:
    name: config
  nodeRegistration:
    kubeletExtraArgs:
      "feature-gates": "EphemeralContainers=true"
nodes:
- role: control-plane
  kubeadmConfigPatches:
  - |
    kind: InitConfiguration
    nodeRegistration:
      kubeletExtraArgs:
        node-labels: "ingress-ready=true"
        eviction-hard: "imagefs.available<1%,nodefs.available<1%"
        eviction-minimum-reclaim: "imagefs.available=1%,nodefs.available=1%"
  extraPortMappings:
  - containerPort: 80
    hostPort: 80
    protocol: TCP
  - containerPort: 443
    hostPort: 443
    protocol: TCP
networking:
  disableDefaultCNI: true
"""

cluster_issuer = '''apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: local
spec:
  ca:
    secretName: ca-key-pair
'''


@k8s.command()
def doctor():
    """Check if you have everything needed to run the stack."""
    docker = which('docker')
    if docker is None:
        raise click.UsageError("You need to install docker")
    if sys.platform == 'linux':
        if 'docker' not in [grp.getgrgid(g).gr_name for g in os.getgroups()]:
            raise click.UsageError("You need to add the current user in the docker group")
    LOGGER.info("We did not find a reason to believe you will have trouble playing with the stack")


@k8s.group(default_command='all')
def install_dependency():
    """Install the dependencies needed to setup the stack"""
    # call(['sudo', 'apt', 'install', 'libnss-myhostname', 'docker.io'])


@install_dependency.command()
@flag('--force', help="Overwrite the existing binaries")
def kind(force):
    """Install kind"""
    if config.k8s.distribution != "kind":
        LOGGER.status(f"I won't try to install kind because you use --distribution={config.k8s.distribution}."
                      " To install kind, run clk k8s --distribution kind install-dependency kind.")
        return
    kind_version = re.search('/(v[0-9.]+)/', kind_url).group(1)
    if not force and not which("kind"):
        force = True
        LOGGER.info("Could not find kind")
    if which("kind"):
        found_kind_version = re.match('kind (v[0-9.]+) .+', check_output(['kind', 'version'])).group(1)
    if not force and found_kind_version != kind_version:
        force = True
        LOGGER.info(f"Found a different version of kind ({found_kind_version}) than the requested one {kind_version}")
    if force:
        download(kind_url, outdir=bin_dir, outfilename='kind', mode=0o755)
    else:
        LOGGER.info("No need to install kind, force with --force")


@install_dependency.command()
@flag('--force', help="Overwrite the existing binaries")
def k3d(force):
    """Install k3d"""
    if config.k8s.distribution != "k3d":
        LOGGER.status(f"I won't try to install k3d because you use --distribution={config.k8s.distribution}."
                      " To install k3d, run clk k8s --distribution k3d install-dependency k3d.")
        return
    k3d_version = re.search('/(v[0-9.]+)/', k3d_url).group(1)
    if not force and not which("k3d"):
        force = True
        LOGGER.info("Could not find k3d")
    if which("k3d"):
        found_k3d_version = re.match('k3d version (.+)', check_output(['k3d', '--version'])).group(1)
    if not force and found_k3d_version != k3d_version:
        force = True
        LOGGER.info(f"Found a different version of k3d ({found_k3d_version}) than the requested one {k3d_version}")
    if force:
        download(k3d_url, outdir=bin_dir, outfilename='k3d', mode=0o755)
    else:
        LOGGER.info("No need to install k3d, force with --force")


@install_dependency.command()
@flag('--force', help="Overwrite the existing binaries")
def helm(force):
    """Install helm"""
    helm_version = re.search('helm-(v[0-9.]+)', helm_url).group(1)
    if not force and not which("helm"):
        force = True
        LOGGER.info("Could not find helm")
    if which("helm"):
        found_helm_version = re.search('Version:"(v[0-9.]+)"', check_output(['helm', 'version'])).group(1)
    if not force and found_helm_version != helm_version:
        force = True
        LOGGER.info(f"Found a different version of helm ({found_helm_version}) than the requested one {helm_version}")
    if force:
        with tempdir() as d:
            extract(helm_url, d)
            move(Path(d) / 'linux-amd64' / 'helm', bin_dir / 'helm')
            (bin_dir / 'helm').chmod(0o755)
    else:
        LOGGER.info("No need to install helm, force with --force")


@install_dependency.command()
@flag('--force', help="Overwrite the existing binaries")
def tilt(force):
    """Install tilt"""
    tilt_version = re.search('/(v[0-9.]+)/', tilt_url).group(1)
    if not force and not which("tilt"):
        force = True
        LOGGER.info("Could not find tilt")
    if which("tilt"):
        found_tilt_version = re.match('(v[0-9.]+)', check_output(['tilt', 'version'])).group(1)
    if not force and found_tilt_version != tilt_version:
        force = True
        LOGGER.info(f"Found a different version of tilt ({found_tilt_version}) than the requested one {tilt_version}")
    if force:
        with tempdir() as d:
            extract(tilt_url, d)
            move(Path(d) / 'tilt', bin_dir / 'tilt')
    else:
        LOGGER.info('No need to install tilt, force with --force')


@install_dependency.command()
@flag('--force', help="Overwrite the existing binaries")
def kubectl(force):
    """Install kubectl"""
    kubectl_version = re.search('/(v[0-9.]+)/', kubectl_url).group(1)
    if not force and not which("kubectl"):
        force = True
        LOGGER.info("Could not find kubectl")
    if which("kubectl"):
        found_kubectl_version = re.match('Client Version: .+ GitVersion:"(v[0-9.]+)"',
                                         check_output(['kubectl', 'version', '--client=true'], failok=True)).group(1)
    if not force and found_kubectl_version != kubectl_version:
        force = True
        LOGGER.info(
            f"Found a different version of kubectl ({found_kubectl_version}) than the requested one {kubectl_version}")
    if force:
        download(kubectl_url, outdir=bin_dir, outfilename='kubectl', mode=0o755)
    else:
        LOGGER.info("No need to install kubectl, force with --force")


@install_dependency.command()
@flag('--force', help="Overwrite the existing binaries")
def kubectl_buildkit(force):
    """Install kubectl buildkit"""
    kubectl_buildkit_version = re.search('/(v[0-9.]+)/', kubectl_buildkit_url).group(1)
    found_kubectl_buildkit_version = False
    try:
        found_kubectl_buildkit_version = check_output(['kubectl', 'buildkit', 'version'])
        found_kubectl_buildkit_version = re.sub(r'\n', '', found_kubectl_buildkit_version)
    except subprocess.CalledProcessError:
        found_kubectl_buildkit_version = False

    if not force and not found_kubectl_buildkit_version:
        force = True
        LOGGER.info("Could not find kubectl buildkit")
    if not force and found_kubectl_buildkit_version != kubectl_buildkit_version:
        force = True
        LOGGER.info(f"Found a different version of kubectl buildkit "
                    f"({found_kubectl_buildkit_version}) than the requested one {kubectl_buildkit_version}")
    if force:
        with tempdir() as d:
            extract(kubectl_buildkit_url, d)
            move(Path(d) / 'kubectl-build', bin_dir / 'kubectl-build')
            move(Path(d) / 'kubectl-buildkit', bin_dir / 'kubectl-buildkit')
    else:
        LOGGER.info("No need to install kubectl buildkit, force with --force")


@install_dependency.command()
@flag('--force', help="Overwrite the existing binaries")
def _all(force):
    """Install all the dependencies"""
    ctx = click.get_current_context()
    ctx.invoke(kubectl, force=force)
    ctx.invoke(kubectl_buildkit, force=force)
    ctx.invoke(helm, force=force)
    ctx.invoke(tilt, force=force)
    ctx.invoke(k3d, force=force)
    ctx.invoke(kind, force=force)


@k8s.command(flowdepends=['k8s.create-cluster'])
@option('--registry-provider', type=click.Choice(['gitlab']), help="What registry provider to connect to")
@option('--username', help="The username of the provider registry")
@option('--password', help="The password of the provider registry")
def install_docker_registry_secret(registry_provider, username, password):
    """Install the credential to get access to the given registry provider."""
    registries = {
        'gitlab': {
            'secret-name': 'gitlab-registry',
            'server': 'registry.gitlab.com',
        }
    }
    if registry_provider:
        if not (username and password):
            if res := get_keyring().get_password('click-project', f'{registry_provider}-registry-auth'):
                username, password = json.loads(res)
        username = username or click.prompt('username', hide_input=True, default='', show_default=False)
        password = password or click.prompt('password', hide_input=True, default='', show_default=False)
        registry = registries[registry_provider]
        config.kubectl.call([
            'create', 'secret', 'docker-registry', registry['secret-name'],
            f'--docker-server={registry["server"]}',
            f'--docker-username={username}',
            f'--docker-password={password}',
        ])  # yapf: disable
    else:
        LOGGER.status("No registry provider given, doing nothing.")


@k8s.command(flowdepends=['k8s.install-dependency.all'])
@flag('--reinstall', help="Reinstall it if it already exists")
def install_local_registry(reinstall):
    """Install k3d local registry"""
    if config.k8s.distribution == "k3d":
        if 'k3d-registry.localhost' in [
                registry['name'] for registry in json.loads(check_output(split('k3d registry list -o json')))
        ]:
            if reinstall:
                ctx = click.get_current_context()
                ctx.invoke(remove, target='registry')
            else:
                LOGGER.info("A registry with the name k3d-registry.localhost already exists." " Nothing to do.")
                return
        call(['k3d', 'registry', 'create', 'registry.localhost', '-p', '5000'])
    else:
        LOGGER.info("We did not think it was useful to install a local registry"
                    f" with the distribution {config.k8s.distribution}."
                    " You might prefer using kubectl build to speed up deployments.")


@k8s.command(flowdepends=['k8s.install-local-registry'])
@flag('--recreate', help="Recreate it if it already exists")
@option(
    '--volume',
    help=("Some local directory that will be made available in the cluster."
          " In docker style format host_path:container_path."
          " Only implemented for k3d for the time being."),
)
def create_cluster(recreate, volume):
    """Create a k3d cluster"""
    if volume and config.k8s.distribution != "k3d":
        LOGGER.warning("--local-volume is only implemented in k3d. It will be ignored.")
    if config.k8s.distribution == "k3d":
        name = 'k3s-default'
        if name in [cluster['name'] for cluster in json.loads(check_output(split('k3d cluster list -o json')))]:
            if recreate:
                call(["k3d", "cluster", "delete", name])
            else:
                LOGGER.info(f"A cluster with the name {name} already exists. Nothing to do.")
                return
    elif config.k8s.distribution == 'kind':
        name = 'kind'
        if name in check_output('kind get clusters'.split()).split('\n'):
            if recreate:
                call(['kind', 'delete', 'clusters', name])
            else:
                LOGGER.info(f"A cluster with the name {name} already exists. Nothing to do.")
                return
    else:
        raise click.ClickException("Unsupported distribution")

    if not is_port_available(80):
        raise click.ClickException("Port 80 is already in use by another process. Please stop this process and retry.")
    if not is_port_available(443):
        raise click.ClickException("Port 443 is already in use by another process. Please stop this process and retry.")

    if config.k8s.distribution == "k3d":
        import yaml
        cmd = [
            'k3d', 'cluster', 'create', name,
            '--wait',
            '--port', '80:80@loadbalancer',
            '--port', '443:443@loadbalancer',
            '--registry-use', 'k3d-registry.localhost:5000',
            '--k3s-agent-arg', '--kubelet-arg=eviction-hard=imagefs.available<1%,nodefs.available<1%',
            '--k3s-agent-arg', '--kubelet-arg=eviction-minimum-reclaim=imagefs.available=1%,nodefs.available=1%',
            '--k3s-server-arg', '--disable-network-policy',
        ]  # yapf: disable
        if volume:
            local_volume = volume.split(":")[0]
            makedirs(local_volume)
            cmd.extend(['--volume', volume])
        call(cmd)
        traefik_conf = ''
        time.sleep(10)
        while not traefik_conf:
            try:
                traefik_conf = config.kubectl.output(['get', 'cm', 'traefik', '-n', 'kube-system', '-o', 'yaml'])
            except subprocess.CalledProcessError:
                time.sleep(5)
        traefik_conf = yaml.load(traefik_conf, Loader=yaml.FullLoader)
        traefik_conf['data']['traefik.toml'] = ('insecureSkipVerify = true\n' + traefik_conf['data']['traefik.toml'])
        with temporary_file() as f:
            f.write(yaml.dump(traefik_conf).encode('utf8'))
            f.close()
            config.kubectl.call(['apply', '-n', 'kube-system', '-f', f.name])
        config.kubectl.call(['delete', 'pod', '-l', 'app=traefik', '-n', 'kube-system'])
    elif config.k8s.distribution == "kind":
        with temporary_file() as f:
            f.write(kind_config.encode('utf8'))
            f.close()
            call(['kind', 'create', 'cluster', '--config', f.name])


@k8s.command(flowdepends=['k8s.install-ingress-nginx'])
@option('--version', default='v1.2.0', help="The version of cert-manager chart to install")
def install_cert_manager(version):
    """Install a certificate manager in the current cluster"""
    call(['helm', 'repo', 'add', 'jetstack', 'https://charts.jetstack.io'])
    call([
        'helm', '--kube-context', config.kubectl.context,
        'upgrade', '--install', '--create-namespace', '--wait', 'cert-manager', 'jetstack/cert-manager',
        '--namespace', 'cert-manager',
        '--version', version,
        '--set', 'installCRDs=true',
        '--set', 'ingressShim.defaultIssuerName=local',
        '--set', 'ingressShim.defaultIssuerKind=ClusterIssuer',
    ])  # yapf: disable
    # generate a certificate authority for the cert-manager
    with tempdir() as d, cd(d):
        ca_key = check_output(['docker', 'run', '--rm', 'alpine/openssl', 'genrsa', '2048'])
        with open("ca.key", "w") as f:
            f.write(ca_key)

        ca_crt = check_output([
            'docker', 'run', '--rm', '--entrypoint', '/bin/sh', 'alpine/openssl', '-c',
            'echo -e "' + '\\n'.join(ca_key.split(sep='\n')) +
            '" | openssl req -x509 -new -nodes -key /dev/stdin -subj /CN=localhost -days 3650' +
            ' -reqexts v3_req -extensions v3_ca',
        ])  # yapf: disable
        with open("ca.crt", "w") as f:
            f.write(ca_crt)

        ca_secret = config.kubectl.output([
            'create', 'secret', 'tls', 'ca-key-pair',
            '--cert=ca.crt',
            '--key=ca.key',
            '--namespace=cert-manager',
            '--dry-run=client',
            '-o', 'yaml',
        ])  # yapf: disable
    with temporary_file() as f:
        f.write(f'''{ca_secret}
---
{cluster_issuer}
'''.encode('utf8'))
        f.close()
        config.kubectl.call(['apply', '-n', 'cert-manager', '-f', f.name])


@k8s.command(flowdepends=['k8s.install-cilium'])
@option('--version', default='v3.35.0', help="The version of ingress-nginx chart to install")
def install_ingress_nginx(version):
    """Install an ingress (ingress-nginx) in the current cluster"""
    if config.k8s.distribution != 'k3d':
        call(['helm', 'repo', 'add', 'ingress-nginx', 'https://kubernetes.github.io/ingress-nginx'])
        helm_extra_args = []
        if config.k8s.distribution == 'kind':
            helm_extra_args += [
                '--set', 'controller.service.type=NodePort',
                '--set', 'controller.hostPort.enabled=true',
            ]  # yapf: disable
        call([
            'helm', '--kube-context', config.kubectl.context,
            'upgrade', '--install', '--create-namespace', '--wait', 'ingress-nginx', 'ingress-nginx/ingress-nginx',
            '--namespace', 'ingress',
            '--version', version,
            '--set', 'rbac.create=true'
        ] + helm_extra_args)  # yapf: disable


@k8s.command()
@option('--version', default='v18.0.2', help="The version of kube-prometheus-stack chart to install")
@option('--alertmanager/--no-alertmanager', help="Enable alertmanager")
@option('--pushgateway/--no-pushgateway', help="Enable pushgateway")
@option('--coredns/--no-coredns', help="Enable coreDns")
@option('--kubedns/--no-kubedns', help="Enable kubeDns")
@option('--kube-scheduler/--no-kube-scheduler', help="Enable kubeScheduler")
@option('--kube-controller-manager/--no-kube-controller-manager', help="Enable kubeControllerManager")
@option('--prometheus-retention', default='1d', help="Server retention")
@option('--prometheus-persistence-size', default='1Gi', help="Prometheus persistent volume size")
@option('--grafana-host', default='grafana.localhost', help="Grafana host")
@option('--grafana-persistence-size', default='1Gi', help="Grafana persistent volume size")
@option('--grafana-admin-password', default='grafana', help="Grafana admin password")
def install_kube_prometheus_stack(version, alertmanager, pushgateway, coredns, kubedns, kube_scheduler,
                                  kube_controller_manager, prometheus_retention, prometheus_persistence_size,
                                  grafana_host, grafana_persistence_size, grafana_admin_password):
    """Install a kube-prometheus-stack instance in the current cluster"""
    call(['helm', 'repo', 'add', 'prometheus-community', 'https://prometheus-community.github.io/helm-charts'])
    call(['helm', 'repo', 'update'])
    call([
        'helm', '--kube-context', config.kubectl.context,
        'upgrade', '--install', '--create-namespace', '--wait', 'kube-prometheus-stack',
        'prometheus-community/kube-prometheus-stack',
        '--namespace', 'monitoring',
        '--version', version,
        '--set', 'alertmanager.enabled=' + str(alertmanager).lower(),
        '--set', 'pushgateway.enabled=' + str(pushgateway).lower(),
        '--set', 'coreDns.enabled=' + str(coredns).lower(),
        '--set', 'kubeDns.enabled=' + str(kubedns).lower(),
        '--set', 'kubeScheduler.enabled=' + str(kube_scheduler).lower(),
        '--set', 'kubeControllerManager.enabled=' + str(kube_controller_manager).lower(),
        '--set', 'prometheus.prometheusSpec.retention=' + prometheus_retention,
        '--set', 'prometheus.prometheusSpec.persistentVolume.size=' + prometheus_persistence_size,
        '--set', 'prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false',
        '--set', 'prometheus-node-exporter.hostRootFsMount=' +
                 str(not (config.k8s.distribution == "docker-desktop")).lower(),
        '--set', 'grafana.ingress.enabled=true',
        '--set', 'grafana.ingress.hosts[0]=' + str(grafana_host),
        '--set', 'grafana.adminPassword=' + str(grafana_admin_password),
        '--set', 'grafana.persistence.enabled=true',
        '--set', 'grafana.persistence.size=' + grafana_persistence_size,
        '--set', 'grafana.deploymentStrategy.type=Recreate',
    ])  # yapf: disable


@k8s.command(flowdepends=['k8s.create-cluster'])
@option('--version', default='v0.50.0', help="The version of prometheus operator CRDs to install")
def install_prometheus_operator_crds(version):
    """Install prometheus operator CRDs in the current cluster"""
    base_url = ('https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/' +
                f'{version}/example/prometheus-operator-crd')
    for crd in [
            'monitoring.coreos.com_alertmanagerconfigs.yaml',
            'monitoring.coreos.com_alertmanagers.yaml',
            'monitoring.coreos.com_podmonitors.yaml',
            'monitoring.coreos.com_probes.yaml',
            'monitoring.coreos.com_prometheuses.yaml',
            'monitoring.coreos.com_prometheusrules.yaml',
            'monitoring.coreos.com_servicemonitors.yaml',
            'monitoring.coreos.com_thanosrulers.yaml',
    ]:
        config.kubectl.output(['apply', '-f', f'{base_url}/{crd}'])


@k8s.command(flowdepends=['k8s.create-cluster'])
@option('--version', default='v0.0.99', help="The version of reloader chart to install")
def install_reloader(version):
    """Install a reloader in the current cluster"""
    call(['helm', 'repo', 'add', 'stakater', 'https://stakater.github.io/stakater-charts'])
    call([
        'helm', '--kube-context', config.kubectl.context,
        'upgrade', '--install', '--create-namespace', '--wait', 'reloader', 'stakater/reloader',
        '--namespace', 'reloader',
        '--version', version,
    ])  # yapf: disable


@k8s.command(flowdepends=['k8s.create-cluster'])
def install_dnsmasq():
    """Install a dnsmasq server resolving *.localhost to 127.0.0.1. Supported OS: macOS."""
    if sys.platform == "darwin":
        call(['brew', 'install', 'dnsmasq'])
        brew_prefix = check_output(['brew', '--prefix']).rstrip("\n")
        with open(brew_prefix + "/etc/dnsmasq.conf", "r+") as f:
            line_found = any("address=/localhost/127.0.0.1" in line for line in f)
            if not line_found:
                f.seek(0, os.SEEK_END)
                f.write("\naddress=/localhost/127.0.0.1\n")
        call(['sudo', 'brew', 'services', 'restart', 'dnsmasq'])
        call(['sudo', 'mkdir', '-p', '/etc/resolver'])
        with temporary_file(content='nameserver 127.0.0.1\n') as f:
            call(['sudo', 'cp', f.name, '/etc/resolver/localhost'])


@k8s.command(flowdepends=['k8s.create-cluster'])
@argument('domain', help="The domain name to define")
@argument('ip', default='172.17.0.1', help="The IP address for this domain")
def add_domain(domain, ip):
    """Add a new domain entry in K8s dns"""
    import yaml

    if config.k8s.distribution == "k3d":
        coredns_conf = config.kubectl.output(['get', 'cm', 'coredns', '-n', 'kube-system', '-o', 'yaml'])
        coredns_conf = yaml.load(coredns_conf, Loader=yaml.FullLoader)
        data = f'{ip} {domain}'
        if data not in coredns_conf['data']['NodeHosts'].split('\n'):
            coredns_conf['data']['NodeHosts'] = data + '\n' + coredns_conf['data']['NodeHosts']
            with temporary_file() as f:
                f.write(yaml.dump(coredns_conf).encode('utf8'))
                f.close()
                config.kubectl.call(['apply', '-n', 'kube-system', '-f', f.name])
    if config.k8s.distribution == "kind":
        coredns_conf = config.kubectl.output(['get', 'cm', 'coredns', '-n', 'kube-system', '-o', 'yaml'])
        coredns_conf = yaml.load(coredns_conf, Loader=yaml.FullLoader)
        top_level_domain = domain.split('.')[-1]
        update = False
        if f'hosts custom.hosts {top_level_domain}' not in coredns_conf['data']['Corefile']:
            data = '''
        hosts custom.hosts %s {
            fallthrough
        }
            '''
            data = data % top_level_domain
            last_bracket_index = coredns_conf['data']['Corefile'].rindex('}')
            coredns_conf['data']['Corefile'] = coredns_conf['data']['Corefile'][0:last_bracket_index] + data + '\n}\n'
            update = True
        data = f'{ip} {domain}'
        header, hosts, footer = re.match(
            r'^(.+hosts custom.hosts ' + top_level_domain + r' \{\n)([^}]*?\n?)(\s+fallthrough\s+\}.+)$',
            coredns_conf['data']['Corefile'], re.DOTALL).groups()
        if f'{data}\n' not in hosts:
            update = True
            coredns_conf['data']['Corefile'] = header + hosts + f'        {data}\n' + footer

        if update:
            with temporary_file() as f:
                f.write(yaml.dump(coredns_conf).encode('utf8'))
                f.close()
                config.kubectl.call(['apply', '-n', 'kube-system', '-f', f.name])
                config.kubectl.call(['rollout', 'restart', '-n', 'kube-system', 'deployment/coredns'])


@k8s.flow_command(flowdepends=[
    'k8s.install-cert-manager',
    'k8s.install-prometheus-operator-crds',
    'k8s.install-network-policy',
])  # yapf: disable
def flow():
    """Run the full k8s setup flow"""
    LOGGER.status('Everything worked well. Now enjoy your new cluster ready to go!')


@k8s.command()
@argument('target', type=click.Choice(['cluster', 'registry', 'all']), default='all', help="What should removed")
def remove(target):
    """Remove the k8s cluster"""
    if config.k8s.distribution == "k3d":
        if target in ['all', 'cluster']:
            call(['k3d', 'cluster', 'delete'])
        if target in ['all', 'registry']:
            call(['k3d', 'registry', 'delete', 'k3d-registry.localhost'])
    elif config.k8s.distribution == "kind":
        if target in ['all', 'cluster']:
            call(['kind', 'delete', 'cluster'])


@k8s.command()
def ipython():
    import IPython

    dict_ = globals()
    dict_.update(locals())
    IPython.start_ipython(argv=[], user_ns=dict_)


class Chart:
    @staticmethod
    def compute_name(metadata):
        return f'{metadata["name"]}-{metadata["version"]}'

    def __init__(self, location):
        self.location = Path(location).resolve()
        self.subcharts_dir = self.location / "charts"
        self.index_path = self.location / "Chart.yaml"
        if not self.index_path.exists():
            raise click.UsageError(f"No file Chart.yaml in the directory {self.location}."
                                   " You must provide as argument the path to a"
                                   " root helm chart directory (meaning with Chart.yaml inside)")
        self.index = yaml.load(self.index_path.open(), Loader=yaml.FullLoader)
        self.name = self.compute_name(self.index)
        self.dependencies = self.index.get('dependencies', [])
        self.dependencies_fullnames = [self.compute_name(dep) for dep in self.dependencies]

    def match_to_dependencies(self, name):
        """Check whether name is fulfilling a dependency of mine

        It can either be the exact name of a dependency, or a prefix of a
        dependency. This allows dependencies like some-dep.develop to be
        fulfilled by the name some-dep.
        """
        return [dependency for dependency in self.dependencies_fullnames if dependency.startswith(name)]

    def package(self, directory=None):
        """Package my content into the specified directory (or by default in the current working directory)"""
        directory = directory or os.getcwd()
        LOGGER.status(f"Packaging {self.name} (from {self.location}) in {directory}")
        with cd(directory):
            call(['helm', 'package', self.location])

    def get_dependencies_with_helm(self, deps_to_update):
        """Use helm to download the given dependencies"""
        # create a copy of Chart.yaml without the dependencies we don't want to redownload
        # in a temporary directory
        LOGGER.status(
            f"Starting to download {', '.join([self.compute_name(dep) for dep in deps_to_update])} for {self.name}")
        chart_to_update = deepcopy(self.index)
        chart_to_update['dependencies'] = deps_to_update
        with tempdir() as d, open(f'{d}/Chart.yaml', 'w') as f:
            yaml.dump(chart_to_update, f)
            # download the dependencies
            LOGGER.status("## The following is some helm logs, don't pay much attention to its gibberish")
            if config.experimental_oci:
                with updated_env(HELM_EXPERIMENTAL_OCI='1'):
                    call(['helm', 'dependency', 'update', d])
            else:
                call(['helm', 'dependency', 'update', d])
            LOGGER.status("## Done with strange helm logs")
            # and move them to the real charts directory
            generated_dependencies = set(os.listdir(f'{d}/charts'))
            for gd in generated_dependencies:
                makedirs(self.subcharts_dir)
                old_path = Path(d) / "charts" / gd
                new_path = self.subcharts_dir / gd
                if new_path.exists():
                    rm(new_path)
                move(old_path, new_path)
        LOGGER.status(f"Downloaded {', '.join([self.compute_name(dep) for dep in deps_to_update])} for {self.name}")
        return generated_dependencies

    @staticmethod
    def find_one_source(dependency, subchart_sources):
        """If one subchart source is able to fulfill the dependency, return it."""
        match = [chart for chart in subchart_sources if dependency.startswith(chart.name)]
        if len(match) > 1:
            raise NotImplementedError()
        if not match:
            return None
        match = match[0]
        if dependency != match.name:
            LOGGER.warning(f"I guessed that the provided package {match.name} (available at {match.location})"
                           f" is a good candidate to fulfill the dependency {dependency}."
                           " Am I wrong?")
        return match

    def update_dependencies(self, subchart_sources, force=False):
        """Make sure the dependencies are up-to-date

        Using the subchart_sources to fulfill the dependencies when possible. It
        does not download dependencies that already are present, unless force is
        set to True.
        """
        to_fetch_with_helm = []
        to_resolve = set()
        updated = False
        if self.dependencies:
            makedirs(self.subcharts_dir)
        for dependency in self.dependencies:
            dependency_name = f"{self.compute_name(dependency)}.tgz"
            src = self.find_one_source(self.compute_name(dependency), subchart_sources)
            if src is not None:
                LOGGER.status(f"Using {src.name} (from {src.location}) to fulfill dependency {dependency_name}")
                src.update_dependencies(subchart_sources, force=force)
                src.package(self.subcharts_dir)
                updated = True
            elif force:
                LOGGER.status(f"I will unconditionally download {dependency_name} as a dependency of {self.name}"
                              " (because of --force)")
                to_fetch_with_helm.append(dependency)
            elif (self.subcharts_dir / dependency_name).exists():
                LOGGER.status(f"{dependency_name} is already an up to date dependency of {self.name}")
                to_resolve.add(self.subcharts_dir / dependency_name)
            else:
                to_fetch_with_helm.append(dependency)
        generated_dependencies = set()
        if to_fetch_with_helm:
            generated_dependencies = self.get_dependencies_with_helm(to_fetch_with_helm)
        if generated_dependencies or to_resolve:
            with tempdir() as d:
                for dependency_to_resolve in generated_dependencies | to_resolve:
                    dependency_chart_location = self.subcharts_dir / dependency_to_resolve
                    temp_dependency_location = d / Path(dependency_to_resolve).name
                    import tarfile
                    with tarfile.open(dependency_chart_location, mode="r:gz") as tar:
                        tar.extractall(temp_dependency_location)
                    dependency_chart = Chart(next(temp_dependency_location.iterdir()))
                    updated_subcharts = dependency_chart.resolve_subcharts(subchart_sources=subchart_sources)
                    if updated_subcharts:
                        LOGGER.status(f"In {self.location}, substituting {dependency_chart.name} by the resolved one")
                        rm(dependency_chart_location)
                        dependency_chart.package(self.subcharts_dir)

            updated = True
        return updated

    def resolve_subcharts(self, subchart_sources):
        updated = False
        if not self.subcharts_dir.exists():
            return updated
        for subchart_dir in self.subcharts_dir.iterdir():
            if subchart_dir.is_dir():
                subchart = Chart(subchart_dir)
                src = self.find_one_source(subchart.name, subchart_sources)
                if src is not None:
                    LOGGER.status(f"Substituting {subchart.location} by the source {src.name} from {src.location}")
                    rm(subchart.location)
                    copy(src.location, subchart.location)
                    updated = True
                else:
                    updated = subchart.resolve_subcharts(subchart_sources=subchart_sources) or updated
        return updated

    def clean_dependencies(self):
        """Remove any archive in the subcharts that is not fulfilling a dependency"""
        for file in self.subcharts_dir.iterdir():
            if file.name.endswith(".tgz") and not self.match_to_dependencies(file.name[:-len(".tgz")]):
                rm(file)

    def __repr__(self):
        return f"<{self.__class__.__name__}('{self.location}')>"


@k8s.command()
@option('--force/--no-force', '-f', help="Force update")
@option('--touch', '-t', help="Touch this file or directory when update is complete")
@option('--experimental-oci/--no-experimental-oci', default=True, help="Activate experimental OCI feature")
@option('subchart_sources',
        '--package',
        '-p',
        multiple=True,
        type=Chart,
        help=('Directory of a helm package that can be used to override the dependency fetching mechanism'))
@option('--remove/--no-remove', default=True, help="Remove extra dependency that may still be there")
@argument('chart', default='.', type=Chart, required=False, help="Helm chart path")
def helm_dependency_update(chart, force, touch, experimental_oci, subchart_sources, remove):
    """Update helm dependencies

    Like `helm dependency update` on steroids.

    It downloads the dependencies, like helm does, but allow you to provide some
    source of nested dependencies that will be packaged on the fly.

    If you provide other chart folders using --package, those that will match
    the dependencies will be packaged instead of downloading the dependency.

    This is done recursively, meaning that you can provide the sources of
    several dependencies and dependencies of dependencies and they will be
    appropriately packages and put one into the other.

    If you work on A, B and C with A depending on B depending on C. You want to
    easily package your project A with the sources of B and C automatically
    packaged into the package A.

    Also, if you work only on C, you'd want that packaging A would substitute on the fly C
    in the dependencies of B.

    That way, you simply have to run helm-dependency-update --package C A and
    you get A/charts/B.tgz that contains an updated C.

    """
    config.experimental_oci = experimental_oci
    updated_something = chart.update_dependencies(subchart_sources, force=force)
    if remove:
        chart.clean_dependencies()
    if touch and updated_something:
        LOGGER.action(f"touching {touch}")
        os.utime(touch)


@k8s.command()
@option('--docker-login/--no-docker-login', '-d', help="Also log into docker")
@option('--helm-login/--no-helm-login', '-h', help="Also log into helm")
@option('--export-password', '-p', help="Export the passwords that directory, with the registry host as name")
@argument('secret', help="K8s secret to use")
def docker_credentials(docker_login, helm_login, secret, export_password):
    """Extract the docker credentials from a k8s secret"""
    creds = config.kubectl.output(
        ['get', 'secret', secret, '--template', '{{index .data ".dockerconfigjson" | base64decode }}'])
    creds = json.loads(creds)
    for registry, values in creds['auths'].items():
        if docker_login:
            check_output(['docker', 'login', registry, '-u', values['username'], '-p', values['password']])
        if helm_login:
            with updated_env(HELM_EXPERIMENTAL_OCI='1'):
                check_output(
                    ['helm', 'registry', 'login', registry, '-u', values['username'], '-p', values['password']])
    if export_password:
        makedirs(export_password)
        for registry, values in creds['auths'].items():
            f_path = f'{export_password}/{registry}'
            if not os.path.exists(f_path) or read(f_path) != values['password']:
                with open(f_path, 'w') as f:
                    LOGGER.action(f'writing to {f_path}')
                    f.write(values['password'])
    print(json.dumps(creds['auths']))


@k8s.command()
@option('--max-parallelism', '-j', default=1, help="Maximum parallelism")
@argument('name', default='buildkit', required=False, help="Runner name")
def create_buildkit_runner(max_parallelism, name):
    """Create a buildkit runner"""
    conf = f'''debug = false
[worker.containerd]
  namespace = "k8s.io"
  max-parallelism = {max_parallelism}
'''
    with temporary_file(content=conf) as f:
        call(['kubectl', 'buildkit', '--context', config.kubectl.context, 'create', '--config', f.name, name])


_features = {
    'kind': {
        'kubectl_build': True
    },
    'k3d': {
        'kubectl_build': False
    },
}


@k8s.command(handle_dry_run=True)
@table_format(default='key_value')
@table_fields(choices=['variable', 'value'])
@argument("keys",
          type=click.Choice(list(_features['kind'].keys())),
          nargs=-1,
          help="Only display these key values. If no key is provided, all the key values are displayed")
def features(fields, format, keys):
    """Show supported features for the current distribution"""
    with TablePrinter(fields, format) as tp:
        fs = _features[config.k8s.distribution]
        keys = keys or sorted(fs.keys())
        for k in keys:
            tp.echo(k, fs[k])


@k8s.command(flowdepends=['k8s.create-cluster'])
def install_cilium():
    """Install cilium"""
    if config.k8s.distribution == "kind":
        # config.kubectl.call(['apply', '-f',
        # 'https://raw.githubusercontent.com/cilium/cilium/v1.9/install/kubernetes/quick-install.yaml'])
        call(['helm', 'repo', 'add', 'cilium', 'https://helm.cilium.io/'])
        call([
            'helm', '--kube-context', config.kubectl.context, 'upgrade', '--install', '--wait',
            'cilium', 'cilium/cilium', '--version', '1.9.10',
            '--namespace', 'kube-system',
            '--set', 'nodeinit.enabled=true',
            '--set', 'kubeProxyReplacement=partial',
            '--set', 'hostServices.enabled=false',
            '--set', 'externalIPs.enabled=true',
            '--set', 'nodePort.enabled=true',
            '--set', 'hostPort.enabled=true',
            '--set', 'bpf.masquerade=false',
            '--set', 'image.pullPolicy=IfNotPresent',
            '--set', 'ipam.mode=kubernetes',
            '--set', 'operator.replicas=1',
        ])  # yapf: disable


network_policy = """kind: NetworkPolicy
apiVersion: networking.k8s.io/v1
metadata:
  name: deny-from-other-namespaces
  namespace: default
spec:
  podSelector: {}
  ingress:
    - from:
        - podSelector: {}
          namespaceSelector:
            matchLabels:
              name: monitoring
        - podSelector: {}
          namespaceSelector:
            matchLabels:
              name: logging
"""

extra_network_policy = """
        - podSelector: {}
          namespaceSelector:
            matchLabels:
              name: ingress
        - podSelector: {}
          namespaceSelector:
            matchLabels:
              name: default
"""


@k8s.command()
@option('--strict/--permissive', help="Whether the network policy is permissive or strict")
def install_network_policy(strict):
    """Isolate the default namespace from the rest"""
    content = network_policy
    if not strict:
        content += extra_network_policy
    with temporary_file(content=content) as f:
        config.kubectl.call(['apply', '-f', f.name])
