#    Copyright 2018 Rackspace, US Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from cryptography import fernet
from jsonschema import exceptions as js_exceptions
from jsonschema import validate

from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging as messaging
from stevedore import driver as stevedore_driver

from octavia_lib.api.drivers import data_models as driver_dm
from octavia_lib.api.drivers import exceptions
from octavia_lib.api.drivers import provider_base as driver_base

from octavia.api.drivers.amphora_driver import availability_zone_schema
from octavia.api.drivers.amphora_driver import flavor_schema
from octavia.api.drivers import utils as driver_utils
from octavia.common import constants as consts
from octavia.common import data_models
from octavia.common import rpc
from octavia.common import utils
from octavia.db import api as db_apis
from octavia.db import repositories
from octavia.network import base as network_base

CONF = cfg.CONF
CONF.import_group('oslo_messaging', 'octavia.common.config')
LOG = logging.getLogger(__name__)
AMPHORA_SUPPORTED_LB_ALGORITHMS = [
    consts.LB_ALGORITHM_ROUND_ROBIN,
    consts.LB_ALGORITHM_SOURCE_IP,
    consts.LB_ALGORITHM_LEAST_CONNECTIONS]


class AmphoraProviderDriver(driver_base.ProviderDriver):
    def __init__(self):
        super(AmphoraProviderDriver, self).__init__()
        self.target = messaging.Target(
            namespace=consts.RPC_NAMESPACE_CONTROLLER_AGENT,
            topic=consts.TOPIC_AMPHORA_V2, version="2.0", fanout=False)
        self.client = rpc.get_client(self.target)
        self.repositories = repositories.Repositories()
        key = utils.get_six_compatible_server_certs_key_passphrase()
        self.fernet = fernet.Fernet(key)

    def _validate_pool_algorithm(self, pool):
        if pool.lb_algorithm not in AMPHORA_SUPPORTED_LB_ALGORITHMS:
            msg = ('Amphora provider does not support %s algorithm.'
                   % pool.lb_algorithm)
            raise exceptions.UnsupportedOptionError(
                user_fault_string=msg,
                operator_fault_string=msg)

    # Load Balancer
    def create_vip_port(self, loadbalancer_id, project_id, vip_dictionary):
        vip_obj = driver_utils.provider_vip_dict_to_vip_obj(vip_dictionary)
        lb_obj = data_models.LoadBalancer(id=loadbalancer_id,
                                          project_id=project_id, vip=vip_obj)

        network_driver = utils.get_network_driver()
        try:
            vip = network_driver.allocate_vip(lb_obj)
        except network_base.AllocateVIPException as e:
            raise exceptions.DriverError(user_fault_string=e.orig_msg,
                                         operator_fault_string=e.orig_msg)

        LOG.info('Amphora provider created VIP port %s for load balancer %s.',
                 vip.port_id, loadbalancer_id)
        return driver_utils.vip_dict_to_provider_dict(vip.to_dict())

    # TODO(johnsom) convert this to octavia_lib constant flavor
    # once octavia is transitioned to use octavia_lib
    def loadbalancer_create(self, loadbalancer):
        if loadbalancer.flavor == driver_dm.Unset:
            loadbalancer.flavor = None
        if loadbalancer.availability_zone == driver_dm.Unset:
            loadbalancer.availability_zone = None
        payload = {consts.LOAD_BALANCER_ID: loadbalancer.loadbalancer_id,
                   consts.FLAVOR: loadbalancer.flavor,
                   consts.AVAILABILITY_ZONE: loadbalancer.availability_zone}
        self.client.cast({}, 'create_load_balancer', **payload)

    def loadbalancer_delete(self, loadbalancer, cascade=False):
        loadbalancer_id = loadbalancer.loadbalancer_id
        payload = {consts.LOAD_BALANCER_ID: loadbalancer_id,
                   'cascade': cascade}
        self.client.cast({}, 'delete_load_balancer', **payload)

    def loadbalancer_failover(self, loadbalancer_id):
        payload = {consts.LOAD_BALANCER_ID: loadbalancer_id}
        self.client.cast({}, 'failover_load_balancer', **payload)

    def loadbalancer_update(self, old_loadbalancer, new_loadbalancer):
        # Adapt the provider data model to the queue schema
        lb_dict = new_loadbalancer.to_dict()
        if 'admin_state_up' in lb_dict:
            lb_dict['enabled'] = lb_dict.pop('admin_state_up')
        lb_id = lb_dict.pop('loadbalancer_id')
        # Put the qos_policy_id back under the vip element the controller
        # expects
        vip_qos_policy_id = lb_dict.pop('vip_qos_policy_id', None)
        if vip_qos_policy_id:
            vip_dict = {"qos_policy_id": vip_qos_policy_id}
            lb_dict["vip"] = vip_dict

        payload = {consts.LOAD_BALANCER_ID: lb_id,
                   consts.LOAD_BALANCER_UPDATES: lb_dict}
        self.client.cast({}, 'update_load_balancer', **payload)

    def _encrypt_listener_dict(self, listener_dict):
        # We need to encrypt the user cert/key data for sending it
        # over messaging.
        if listener_dict.get(consts.DEFAULT_TLS_CONTAINER_DATA, False):
            listener_dict[consts.DEFAULT_TLS_CONTAINER_DATA] = (
                self.fernet.encrypt(
                    listener_dict[consts.DEFAULT_TLS_CONTAINER_DATA]))
        if listener_dict.get(consts.SNI_CONTAINER_DATA, False):
            sni_list = []
            for sni_data in listener_dict[consts.SNI_CONTAINER_DATA]:
                sni_list.append(self.fernet.encrypt(sni_data))
            if sni_list:
                listener_dict[consts.SNI_CONTAINER_DATA] = sni_list

    # Listener
    def listener_create(self, listener):
        payload = {consts.LISTENER: listener.to_dict()}
        self._encrypt_listener_dict(payload)

        self.client.cast({}, 'create_listener', **payload)

    def listener_delete(self, listener):
        payload = {consts.LISTENER: listener.to_dict()}
        self.client.cast({}, 'delete_listener', **payload)

    def listener_update(self, old_listener, new_listener):
        original_listener = old_listener.to_dict()
        listener_updates = new_listener.to_dict()

        self._encrypt_listener_dict(original_listener)
        self._encrypt_listener_dict(listener_updates)

        payload = {consts.ORIGINAL_LISTENER: original_listener,
                   consts.LISTENER_UPDATES: listener_updates}
        self.client.cast({}, 'update_listener', **payload)

    # Pool
    def _pool_convert_to_dict(self, pool):
        pool_dict = pool.to_dict(recurse=True)
        if 'admin_state_up' in pool_dict:
            pool_dict['enabled'] = pool_dict.pop('admin_state_up')
        if 'tls_container_ref' in pool_dict:
            pool_dict['tls_container_id'] = pool_dict.pop('tls_container_ref')
        pool_dict.pop('tls_container_data', None)
        if 'ca_tls_container_ref' in pool_dict:
            pool_dict['ca_tls_certificate_id'] = pool_dict.pop(
                'ca_tls_container_ref')
        pool_dict.pop('ca_tls_container_data', None)
        if 'client_crl_container_ref' in pool_dict:
            pool_dict['client_crl_container_id'] = pool_dict.pop(
                'client_crl_container_ref')
        pool_dict.pop('client_crl_container_data', None)
        return pool_dict

    def pool_create(self, pool):
        self._validate_pool_algorithm(pool)
        payload = {consts.POOL: self._pool_convert_to_dict(pool)}
        self.client.cast({}, 'create_pool', **payload)

    def pool_delete(self, pool):
        payload = {consts.POOL: pool.to_dict(recurse=True)}
        self.client.cast({}, 'delete_pool', **payload)

    def pool_update(self, old_pool, new_pool):
        if new_pool.lb_algorithm:
            self._validate_pool_algorithm(new_pool)
        pool_dict = self._pool_convert_to_dict(new_pool)
        pool_dict.pop('pool_id')
        payload = {consts.ORIGINAL_POOL: old_pool.to_dict(),
                   consts.POOL_UPDATES: pool_dict}
        self.client.cast({}, 'update_pool', **payload)

    # Member
    def member_create(self, member):
        pool_id = member.pool_id
        db_pool = self.repositories.pool.get(db_apis.get_session(),
                                             id=pool_id)
        self._validate_members(db_pool, [member])

        payload = {consts.MEMBER: member.to_dict()}
        self.client.cast({}, 'create_member', **payload)

    def member_delete(self, member):
        payload = {consts.MEMBER: member.to_dict()}
        self.client.cast({}, 'delete_member', **payload)

    def member_update(self, old_member, new_member):
        original_member = old_member.to_dict()
        member_updates = new_member.to_dict()
        if 'admin_state_up' in member_updates:
            member_updates['enabled'] = member_updates.pop('admin_state_up')
        member_updates.pop(consts.MEMBER_ID)
        payload = {consts.ORIGINAL_MEMBER: original_member,
                   consts.MEMBER_UPDATES: member_updates}
        self.client.cast({}, 'update_member', **payload)

    def member_batch_update(self, pool_id, members):
        # The DB should not have updated yet, so we can still use the pool
        db_pool = self.repositories.pool.get(db_apis.get_session(), id=pool_id)

        self._validate_members(db_pool, members)

        old_members = db_pool.members

        old_member_ids = [m.id for m in old_members]
        # The driver will always pass objects with IDs.
        new_member_ids = [m.member_id for m in members]

        # Find members that are brand new or updated
        new_members = []
        updated_members = []
        for m in members:
            if m.member_id not in old_member_ids:
                new_members.append(m)
            else:
                member_dict = m.to_dict(render_unsets=False)
                member_dict['id'] = member_dict.pop('member_id')
                if 'address' in member_dict:
                    member_dict['ip_address'] = member_dict.pop('address')
                if 'admin_state_up' in member_dict:
                    member_dict['enabled'] = member_dict.pop('admin_state_up')
                updated_members.append(member_dict)

        # Find members that are deleted
        deleted_members = []
        for m in old_members:
            if m.id not in new_member_ids:
                deleted_members.append(m)

        if deleted_members or new_members or updated_members:
            payload = {'old_members': [m.to_dict() for m in deleted_members],
                       'new_members': [m.to_dict() for m in new_members],
                       'updated_members': updated_members}
            self.client.cast({}, 'batch_update_members', **payload)
        else:
            LOG.info("Member batch update is a noop, returning early.")

    def _validate_members(self, db_pool, members):
        if db_pool.protocol == consts.PROTOCOL_UDP:
            # For UDP LBs, check that we are not mixing IPv4 and IPv6
            for member in members:
                member_is_ipv6 = utils.is_ipv6(member.address)

                for listener in db_pool.listeners:
                    lb = listener.load_balancer
                    vip_is_ipv6 = utils.is_ipv6(lb.vip.ip_address)

                    if member_is_ipv6 != vip_is_ipv6:
                        msg = ("This provider doesn't support mixing IPv4 and "
                               "IPv6 addresses for its VIP and members in UDP "
                               "load balancers.")
                        raise exceptions.UnsupportedOptionError(
                            user_fault_string=msg,
                            operator_fault_string=msg)

    # Health Monitor
    def health_monitor_create(self, healthmonitor):
        payload = {consts.HEALTH_MONITOR_ID: healthmonitor.healthmonitor_id}
        self.client.cast({}, 'create_health_monitor', **payload)

    def health_monitor_delete(self, healthmonitor):
        healthmonitor_id = healthmonitor.healthmonitor_id
        payload = {consts.HEALTH_MONITOR_ID: healthmonitor_id}
        self.client.cast({}, 'delete_health_monitor', **payload)

    def health_monitor_update(self, old_healthmonitor, new_healthmonitor):
        healthmon_dict = new_healthmonitor.to_dict()
        if 'admin_state_up' in healthmon_dict:
            healthmon_dict['enabled'] = healthmon_dict.pop('admin_state_up')
        if 'max_retries_down' in healthmon_dict:
            healthmon_dict['fall_threshold'] = healthmon_dict.pop(
                'max_retries_down')
        if 'max_retries' in healthmon_dict:
            healthmon_dict['rise_threshold'] = healthmon_dict.pop(
                'max_retries')
        healthmon_id = healthmon_dict.pop('healthmonitor_id')

        payload = {consts.HEALTH_MONITOR_ID: healthmon_id,
                   consts.HEALTH_MONITOR_UPDATES: healthmon_dict}
        self.client.cast({}, 'update_health_monitor', **payload)

    # L7 Policy
    def l7policy_create(self, l7policy):
        payload = {consts.L7POLICY_ID: l7policy.l7policy_id}
        self.client.cast({}, 'create_l7policy', **payload)

    def l7policy_delete(self, l7policy):
        l7policy_id = l7policy.l7policy_id
        payload = {consts.L7POLICY_ID: l7policy_id}
        self.client.cast({}, 'delete_l7policy', **payload)

    def l7policy_update(self, old_l7policy, new_l7policy):
        l7policy_dict = new_l7policy.to_dict()
        if 'admin_state_up' in l7policy_dict:
            l7policy_dict['enabled'] = l7policy_dict.pop('admin_state_up')
        l7policy_id = l7policy_dict.pop('l7policy_id')

        payload = {consts.L7POLICY_ID: l7policy_id,
                   consts.L7POLICY_UPDATES: l7policy_dict}
        self.client.cast({}, 'update_l7policy', **payload)

    # L7 Rule
    def l7rule_create(self, l7rule):
        payload = {consts.L7RULE_ID: l7rule.l7rule_id}
        self.client.cast({}, 'create_l7rule', **payload)

    def l7rule_delete(self, l7rule):
        l7rule_id = l7rule.l7rule_id
        payload = {consts.L7RULE_ID: l7rule_id}
        self.client.cast({}, 'delete_l7rule', **payload)

    def l7rule_update(self, old_l7rule, new_l7rule):
        l7rule_dict = new_l7rule.to_dict()
        if 'admin_state_up' in l7rule_dict:
            l7rule_dict['enabled'] = l7rule_dict.pop('admin_state_up')
        l7rule_id = l7rule_dict.pop('l7rule_id')

        payload = {consts.L7RULE_ID: l7rule_id,
                   consts.L7RULE_UPDATES: l7rule_dict}
        self.client.cast({}, 'update_l7rule', **payload)

    # Flavor
    def get_supported_flavor_metadata(self):
        """Returns the valid flavor metadata keys and descriptions.

        This extracts the valid flavor metadata keys and descriptions
        from the JSON validation schema and returns it as a dictionary.

        :return: Dictionary of flavor metadata keys and descriptions.
        :raises DriverError: An unexpected error occurred.
        """
        try:
            props = flavor_schema.SUPPORTED_FLAVOR_SCHEMA['properties']
            return {k: v.get('description', '') for k, v in props.items()}
        except Exception as e:
            raise exceptions.DriverError(
                user_fault_string='Failed to get the supported flavor '
                                  'metadata due to: {}'.format(str(e)),
                operator_fault_string='Failed to get the supported flavor '
                                      'metadata due to: {}'.format(str(e)))

    def validate_flavor(self, flavor_dict):
        """Validates flavor profile data.

        This will validate a flavor profile dataset against the flavor
        settings the amphora driver supports.

        :param flavor_dict: The flavor dictionary to validate.
        :type flavor: dict
        :return: None
        :raises DriverError: An unexpected error occurred.
        :raises UnsupportedOptionError: If the driver does not support
          one of the flavor settings.
        """
        try:
            validate(flavor_dict, flavor_schema.SUPPORTED_FLAVOR_SCHEMA)
        except js_exceptions.ValidationError as e:
            error_object = ''
            if e.relative_path:
                error_object = '{} '.format(e.relative_path[0])
            raise exceptions.UnsupportedOptionError(
                user_fault_string='{0}{1}'.format(error_object, e.message),
                operator_fault_string=str(e))
        except Exception as e:
            raise exceptions.DriverError(
                user_fault_string='Failed to validate the flavor metadata '
                                  'due to: {}'.format(str(e)),
                operator_fault_string='Failed to validate the flavor metadata '
                                      'due to: {}'.format(str(e)))
        compute_flavor = flavor_dict.get(consts.COMPUTE_FLAVOR, None)
        if compute_flavor:
            compute_driver = stevedore_driver.DriverManager(
                namespace='octavia.compute.drivers',
                name=CONF.controller_worker.compute_driver,
                invoke_on_load=True
            ).driver

            # TODO(johnsom) Fix this to raise a NotFound error
            # when the octavia-lib supports it.
            compute_driver.validate_flavor(compute_flavor)

    # Availability Zone
    def get_supported_availability_zone_metadata(self):
        """Returns the valid availability zone metadata keys and descriptions.

        This extracts the valid availability zone metadata keys and
        descriptions from the JSON validation schema and returns it as a
        dictionary.

        :return: Dictionary of availability zone metadata keys and descriptions
        :raises DriverError: An unexpected error occurred.
        """
        try:
            props = (
                availability_zone_schema.SUPPORTED_AVAILABILITY_ZONE_SCHEMA[
                    'properties'])
            return {k: v.get('description', '') for k, v in props.items()}
        except Exception as e:
            raise exceptions.DriverError(
                user_fault_string='Failed to get the supported availability '
                                  'zone metadata due to: {}'.format(str(e)),
                operator_fault_string='Failed to get the supported '
                                      'availability zone metadata due to: '
                                      '{}'.format(str(e)))

    def validate_availability_zone(self, availability_zone_dict):
        """Validates availability zone profile data.

        This will validate an availability zone profile dataset against the
        availability zone settings the amphora driver supports.

        :param availability_zone_dict: The availability zone dict to validate.
        :type availability_zone_dict: dict
        :return: None
        :raises DriverError: An unexpected error occurred.
        :raises UnsupportedOptionError: If the driver does not support
          one of the availability zone settings.
        """
        try:
            validate(
                availability_zone_dict,
                availability_zone_schema.SUPPORTED_AVAILABILITY_ZONE_SCHEMA)
        except js_exceptions.ValidationError as e:
            error_object = ''
            if e.relative_path:
                error_object = '{} '.format(e.relative_path[0])
            raise exceptions.UnsupportedOptionError(
                user_fault_string='{0}{1}'.format(error_object, e.message),
                operator_fault_string=str(e))
        except Exception as e:
            raise exceptions.DriverError(
                user_fault_string='Failed to validate the availability zone '
                                  'metadata due to: {}'.format(str(e)),
                operator_fault_string='Failed to validate the availability '
                                      'zone metadata due to: {}'.format(str(e))
            )
        compute_zone = availability_zone_dict.get(consts.COMPUTE_ZONE, None)
        if compute_zone:
            compute_driver = stevedore_driver.DriverManager(
                namespace='octavia.compute.drivers',
                name=CONF.controller_worker.compute_driver,
                invoke_on_load=True
            ).driver

            # TODO(johnsom) Fix this to raise a NotFound error
            # when the octavia-lib supports it.
            compute_driver.validate_availability_zone(compute_zone)
