#
# INTEL CONFIDENTIAL
# Copyright (c) 2018 Intel Corporation
#
# The source code contained or described herein and all documents related to
# the source code ("Material") are owned by Intel Corporation or its suppliers
# or licensors. Title to the Material remains with Intel Corporation or its
# suppliers and licensors. The Material contains trade secrets and proprietary
# and confidential information of Intel or its suppliers and licensors. The
# Material is protected by worldwide copyright and trade secret laws and treaty
# provisions. No part of the Material may be used, copied, reproduced, modified,
# published, uploaded, posted, transmitted, distributed, or disclosed in any way
# without Intel's prior express written permission.
#
# No license under any patent, copyright, trade secret or other intellectual
# property right is granted to or conferred upon you by disclosure or delivery
# of the Materials, either expressly, by implication, inducement, estoppel or
# otherwise. Any license under such intellectual property rights must be express
# and approved by Intel in writing.
#

import argparse
import glob
import logging
import os

import yaml
from kubernetes import config, client

PREFIX_VALUES = {"E": 10 ** 18, "P": 10 ** 15, "T": 10 ** 12, "G": 10 ** 9, "M": 10 ** 6, "K": 10 ** 3}
PREFIX_I_VALUES = {"Ei": 2 ** 60, "Pi": 2 ** 50, "Ti": 2 ** 40, "Gi": 2 ** 30, "Mi": 2 ** 20, "Ki": 2 ** 10}

HOROVOD_PACKS = {'multinode-tf-training-horovod', 'multinode-tf-training-horovod-py2'}
MULTINODE_PACKS = {'multinode-tf-training-tfjob', 'multinode-tf-training-tfjob-py2'}

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class SimpleDumper(yaml.Dumper):
    def ignore_aliases(self, data):
        return True


def convert_k8s_cpu_resource(cpu_resource: str) -> float:
    # If CPU resources are gives as for example 100m, we simply strip last character and sum leftover numbers.
    if cpu_resource[-1] == "m":
        return int(cpu_resource[:-1])
    # Else we assume that cpu resources are given as float value of normal CPUs instead of miliCPUs.
    else:
        return int(float(cpu_resource) * 1000)


def convert_k8s_memory_resource(mem_resource: str) -> int:
    # If last character is "i" then assume that resource is given as for example 1000Ki.
    if mem_resource[-1] == "i" and mem_resource[-2:] in PREFIX_I_VALUES:
        prefix = mem_resource[-2:]
        return int(mem_resource[:-2]) * PREFIX_I_VALUES[prefix]
    # If last character is one of the normal exponent prefixes (with base 10) then assume that resource is given
    # as for example 1000K.
    elif mem_resource[-1] in PREFIX_VALUES:
        prefix = mem_resource[-1]
        return int(mem_resource[:-1]) * PREFIX_VALUES[prefix]
    # If there is e contained inside resource string then assume that it is given in exponential format.
    elif "e" in mem_resource:
        return int(float(mem_resource))
    else:
        return int(mem_resource)


def get_k8s_api() -> client.CoreV1Api:
    config.load_kube_config()
    return client.CoreV1Api(client.ApiClient())


def get_k8s_worker_allocatable_resources(cpu_threshold: 0.2, mem_threshold: 0.2):
    api = get_k8s_api()
    nodes = api.list_node()
    allocatable_cpus_per_node = []
    allocatable_memory_per_node = []
    for node in nodes.items:
        allocatable_cpus_per_node.append(node.status.allocatable['cpu'])
        allocatable_memory_per_node.append(node.status.allocatable['memory'])

    cpu_request, memory_request = min(allocatable_cpus_per_node), min(allocatable_memory_per_node)
    cpu_limit, memory_limit = max(allocatable_cpus_per_node), max(allocatable_memory_per_node)

    # Convert values
    cpu_request, memory_request = convert_k8s_cpu_resource(cpu_request), convert_k8s_memory_resource(memory_request)
    cpu_limit, memory_limit = convert_k8s_cpu_resource(cpu_limit), convert_k8s_memory_resource(memory_limit)

    # Apply thresholds
    cpu_request *= cpu_threshold
    cpu_request //= 1000
    cpu_limit *= cpu_threshold
    cpu_limit //= 1000

    memory_request *= mem_threshold
    memory_limit *= mem_threshold

    return {'resources': {
                'requests': {'cpu': cpu_request, 'memory': memory_request},
                'limit': {'cpu': cpu_limit, 'memory': memory_limit}
            }}


def get_fixed_resources(fixed_cpu: str, fixed_memory: str):
    return {'resources': {
        'requests': {'cpu': fixed_cpu, 'memory': fixed_memory},
        'limit': {'cpu': fixed_cpu, 'memory': fixed_memory}
    }}


def get_multinode_resources(k8s_worker_resources: dict):
    return {
        'worker_resources': k8s_worker_resources['resources'].copy(),
        'ps_resources': k8s_worker_resources['resources'].copy()
    }


def get_horovod_resources(k8s_worker_resources: dict, physical_cpus=1):
    horovod_resources = k8s_worker_resources.copy()
    horovod_resources['cpus'] = physical_cpus
    return horovod_resources


def override_default_resources_in_packs(dlsctl_config_dir_path: str, k8s_worker_resources: dict, horovod_cpus=1):
    values_yaml_paths = f'{dlsctl_config_dir_path}/packs/*/Charts/values.yaml'
    for values_yaml_path in glob.glob(values_yaml_paths):
        logger.info(f'Changing resources for pack: {values_yaml_path}')
        pack_resources = k8s_worker_resources.copy()
        if any(horovod_pack in values_yaml_path for horovod_pack in HOROVOD_PACKS):
            pack_resources = get_horovod_resources(pack_resources, physical_cpus=horovod_cpus)
        elif any(multinode_pack in values_yaml_path for multinode_pack in MULTINODE_PACKS):
            pack_resources = get_multinode_resources(pack_resources)

        logger.info(f'Calculated resources: {pack_resources}')

        with open(values_yaml_path, mode='r') as values_yaml_file:
            pack_values = yaml.safe_load(values_yaml_file)
            if not pack_values:
                logger.error(f'{values_yaml_path} file empty!')
                raise ValueError
            try:
                updated_pack_values = {**pack_values, **pack_resources}
            except Exception:
                logger.exception('Failed to update values.yaml with calculated resources.')
                exit(1)

        with open(values_yaml_path, mode='w') as values_yaml_file:
            yaml.dump(updated_pack_values, values_yaml_file, default_flow_style=False, Dumper=SimpleDumper)
            logger.info(f'Resources for pack: {values_yaml_path} were changed.\n')


def argument_parser():
    parser = argparse.ArgumentParser(description='Override default pack resources according to resources '
                                                 'actually available on the cluster, or values provided as parameters.')
    parser.add_argument('-c', '--config-dir-path', default=os.environ.get('DLS_CTL_CONFIG'),
                        type=str, help='Path to dlsctl config directory.'
                                       ' May be set by DLS_CTL_CONFIG environment variable.')
    parser.add_argument('--cpu-percentage',
                        help='Define how many percent of allocatable CPUs may be used by packs (e.g. 0.2 or 0.5)',
                        type=float, required=False)
    parser.add_argument('--mem-percentage',
                        help='Define how much allocatable memory may be used by packs (e.g. 0.2 or 0.5)',
                        type=float, required=False)
    parser.add_argument('--cpu-fixed',
                        help='Set fixed CPU requests and limits in packs (e.g. 1 or 4000Mi)',
                        type=str, default='1', required=False)
    parser.add_argument('--mem-fixed',
                        help='Set fixed amount of memory in packs (e.g. 100Mi or 200Gi)',
                        type=str, default='100Mi', required=False)
    parser.add_argument('--cpu-horovod',
                        help='Set desired number of physical CPUs for Horovod (e.g. 1 or 20)',
                        type=int, default=1, required=False)
    return parser

if __name__ == '__main__':
    parser = argument_parser()
    args = parser.parse_args()
    if not args.config_dir_path:
        exit(parser.print_help())

    if args.cpu_fixed and args.mem_fixed:
        resources_to_set = get_fixed_resources(fixed_cpu=args.cpu_fixed, fixed_memory=args.mem_fixed)
    else:
        resources_to_set = get_k8s_worker_allocatable_resources(cpu_threshold=args.cpu_percentage,
                                                                mem_threshold=args.mem_percentage)

    override_default_resources_in_packs(dlsctl_config_dir_path=args.config_dir_path,
                                        k8s_worker_resources=resources_to_set,
                                        horovod_cpus=args.cpu_horovod)
