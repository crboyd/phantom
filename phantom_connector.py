# --
# File: phantom_connector.py
#
# Copyright (c) Phantom Cyber Corporation, 2016
#
# This unpublished material is proprietary to Phantom Cyber.
# All rights reserved. The methods and
# techniques described herein are considered trade secrets
# and/or confidential. Reproduction or distribution, in whole
# or in part, is forbidden except by express written permission
# of Phantom Cyber.
#
# --

# Phantom imports
import phantom.app as phantom

from phantom.base_connector import BaseConnector
from phantom.action_result import ActionResult

from phantom.cef import CEF_NAME_MAPPING
from phantom.utils import CONTAINS_VALIDATORS
import phantom.utils as ph_utils
from phantom.vault import Vault

import ast
import json
import requests
from requests.exceptions import Timeout, SSLError

import socket
from bs4 import BeautifulSoup
import os
import zipfile
import magic
import tarfile
import gzip
import bz2

TIMEOUT = 120
INVALID_RESPONSE = 'Server did not return a valid JSON response.'
SUPPORTED_FILES = ['application/zip', 'application/x-gzip', 'application/x-tar', 'application/x-bzip2']


def determine_contains(value):
    for c, f in CONTAINS_VALIDATORS.items():
        if f(value):
            return c
    return None


class RetVal3(tuple):
    def __new__(cls, val1, val2=None, val3=None):
        return tuple.__new__(RetVal3, (val1, val2, val3))


class PhantomConnector(BaseConnector):

    """
    def _do_request(self, url, method=GET, payload=None):

        # This function returns different TYPES of objects, highly un-maintainable code.
        # Need to replace this one with a _make_rest_call from another app, better error handling
        try:
            if method == GET:
                response = requests.get(url, verify=self.verify_cert, auth=self.use_auth, headers=self.headers, timeout=TIMEOUT)
            elif method == POST:
                response = requests.post(url, data=payload, verify=self.verify_cert, auth=self.use_auth, headers=self.headers, timeout=TIMEOUT)
            else:
                raise ValueError('Invalid method {}'.format(method))
        except Timeout as e:
            raise Exception('HTTP GET request timed out: ' + str(e))
        except SSLError as e:
            raise Exception('HTTPS SSL validation failed: ' + str(e))
        else:
            if response.status_code != 200:
                message = INVALID_RESPONSE
                try:
                    message = response.json()['message']
                except:
                    pass
                return False, (response, message)
        return True, response.json()
        """

    def _get_error_details(self, resp_json):

        # The device that this app talks to does not sends back a simple message,
        # so this function does not need to be that complicated
        return resp_json.get('message', '-')

    def _process_html_response(self, response, action_result):

        # An html response, is bound to be an error
        status_code = response.status_code

        try:
            soup = BeautifulSoup(response.text, "html.parser")
            error_text = soup.text
            split_lines = error_text.split('\n')
            split_lines = [x.strip() for x in split_lines if x.strip()]
            error_text = '\n'.join(split_lines)
        except:
            error_text = "Cannot parse error details"

        message = "Status Code: {0}. Data from server:\n{1}\n".format(status_code,
                error_text)

        # In 2.0 the platform does not like braces in messages, unless it's format parameters
        message = message.replace('{', ' ').replace('}', ' ')

        return RetVal3(action_result.set_status(phantom.APP_ERROR, message), response)

    def _process_json_response(self, response, action_result):

        # Try a json parse
        try:
            resp_json = response.json()
        except Exception as e:
            return RetVal3(action_result.set_status(phantom.APP_ERROR, "Unable to parse response as JSON", e), response)

        failed = resp_json.get('failed', False)

        if (failed):
            return RetVal3(
                    action_result.set_status(phantom.APP_ERROR, "Error from server. Status code: {0}, Details: {1} ".format(response.status_code,
                        self._get_error_details(resp_json))), response)

        if (200 <= response.status_code < 399):
            return RetVal3(phantom.APP_SUCCESS, response, resp_json)

        return RetVal3(
                action_result.set_status(phantom.APP_ERROR, "Error from server. Status code: {0}, Details: {1} ".format(response.status_code,
                    self._get_error_details(resp_json))), response, None)

    def _process_response(self, response, action_result):

        # store the r_text in debug data, it will get dumped in the logs if an error occurs
        if hasattr(action_result, 'add_debug_data'):
            if (response is not None):
                action_result.add_debug_data({'r_text': response.text})
                action_result.add_debug_data({'r_headers': response.headers})
                action_result.add_debug_data({'r_status_code': response.status_code})
            else:
                action_result.add_debug_data({'r_text': 'response is None'})

        # There are just too many differences in the response to handle all of them in the same function
        if (('json' in response.headers.get('Content-Type', '')) or ('javascript' in response.headers.get('Content-Type'))):
            return self._process_json_response(response, action_result)

        if ('html' in response.headers.get('Content-Type', '')):
            return self._process_html_response(response, action_result)

        # it's not an html or json, handle if it is a successfull empty reponse
        if (200 <= response.status_code < 399) and (not response.text):
            return RetVal3(phantom.APP_SUCCESS, response, action_result)

        # everything else is actually an error at this point
        message = "Can't process resonse from server. Status Code: {0} Data from server: {1}".format(
                response.status_code, response.text.replace('{', ' ').replace('}', ' '))

        return RetVal3(action_result.set_status(phantom.APP_ERROR, message), response, None)

    def _make_rest_call(self, endpoint, action_result, headers=None, params=None, data=None, method="get"):

        config = self.get_config()

        # Create the headers
        if (headers is None):
            headers = {}

        if headers:
            try:
                headers = json.loads(headers)
            except Exception as e:
                return action_result.set_status(phantom.APP_ERROR, "Unable to load headers as JSON", e)

        # auth_token is a bit tricky, it can be in the params or config
        auth_token = config.get('auth_token')

        if ((auth_token) and ('ph-auth-token' not in headers)):
                headers['ph-auth-token'] = auth_token

        if ('Content-Type' not in headers):
            headers.update({'Content-Type': 'application/json'})

        request_func = getattr(requests, method)

        if (not request_func):
            action_result.set_status(phantom.APP_ERROR, "Unsupported HTTP method '{0}' requested".format(method))

        try:
            response = request_func(self._base_uri + endpoint,
                    auth=self._auth,
                    json=data,
                    headers=headers if (headers) else None,
                    verify=self._verify_cert,
                    params=params,
                    timeout=TIMEOUT)
        except Timeout as e:
            return RetVal3(action_result.set_status(phantom.APP_ERROR, "Request timed out", e), None, None)
        except SSLError as e:
            return (action_result.set_status(phantom.APP_ERROR, "HTTPS SSL validation failed", e), None, None)
        except Exception as e:
            return (action_result.set_status(phantom.APP_ERROR, "Error connecting to server", e), None, None)

        return self._process_response(response, action_result)

    def _test_connectivity(self, param):

        action_result = ActionResult(param)

        ret_val, response, resp_data = self._make_rest_call('/rest/version', action_result)

        if (phantom.is_fail(ret_val)):
            return self.set_status(phantom.APP_ERROR, 'Failed to connect: {}'.format(action_result.get_message()))

        version = resp_data['version']
        self.save_progress("Connected to Phantom appliance version {}".format(version))
        self.save_progress("Test connectivity PASSED.")
        return self.set_status(phantom.APP_SUCCESS, 'Request succeeded')

    def _find_artifacts(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))

        values = param.get('values', '')

        if param.get('is_regex'):
            flt = 'iregex'
        else:
            flt = 'icontains'

        exact_match = param.get('exact_match')

        if exact_match:
            values = '"{}"'.format(values)

        endpoint = '/rest/artifact?_filter_cef__{}={}&page_size=0&pretty'.format(flt, repr(values))

        ret_val, response, resp_data = self._make_rest_call(endpoint, action_result)

        if phantom.is_fail(ret_val):
            return action_result.set_status(phantom.APP_ERROR, 'Error retrieving records: {0}'.format(action_result.get_message()))

        records = resp_data['data']

        values = values.lower()

        for rec in records:
            key, value = None, None

            for k, v in rec['cef'].iteritems():
                if values in v.lower() or (exact_match and values.strip('"') == v.lower()):
                    key = k
                    value = v
                    break
            result = {
                "id": rec['id'],
                "container": rec['container'],
                "container_name": rec['_pretty_container'],
                "name": rec.get('name'),
                "found in": key if key else "N/A",
                "matched": value if value else "",
            }
            action_result.add_data(result)

        action_result.update_summary({'artifacts found': len(records), 'server': self._base_uri})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _add_artifact(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))

        name = param.get('name')
        container_id = param.get('container_id', self.get_container_id())
        label = param.get('label', 'event')
        contains = param.get('contains', '').strip().split(',')
        cef_name = param.get('cef_name')
        cef_value = param.get('cef_value')

        artifact = {}
        artifact['name'] = name
        artifact['label'] = label
        artifact['container_id'] = container_id
        artifact['cef'] = {
            cef_name: cef_value,
        }

        if contains:
            artifact['cef_types'] = {'cef_name': contains}
        elif cef_name not in CEF_NAME_MAPPING:
            contains = determine_contains(cef_value)
            if contains:
                artifact['cef_types'] = {'cef_name': [contains]}

        success, response, resp_data = self._make_rest_call('/rest/artifact', action_result, method='post', data=artifact)

        if (phantom.is_fail(success)):
            artifact_id = resp_data.get('existing_artifact_id')
            if not artifact_id:
                return action_result.get_status()
        else:
            artifact_id = resp_data.get('id')

        action_result.add_data(resp_data)

        action_result.update_summary({'artifact id': artifact_id, 'container id': container_id, 'server': self._base_uri})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _add_file_to_vault(self, action_result, data_stream, file_name, recursive, container_id):

        save_as = file_name or '_invalid_file_name_'

        # if the path contains a directory
        if (os.path.dirname(save_as)):
            save_as = '-'.join(os.path.split(save_as))

        save_path = os.path.join('/vault/tmp', save_as)
        with open(save_path, 'w') as uncompressed_file:
            uncompressed_file.write(data_stream)

        try:
            vault_info = Vault.add_attachment(save_path, container_id, file_name)
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, "Failed to add file into vault", e)

        if (vault_info.get('failed', False)):
            return action_result.set_status(phantom.APP_ERROR, "Failed to add file into vault, {0}".format(vault_info.get('message', 'NA')))

        try:
            vault_info = Vault.get_file_info(vault_id=vault_info['vault_id'])[0]
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, "Failed to add file info of file added to vault", e)

        action_result.add_data(vault_info)

        if (recursive):

            file_path = vault_info['path']

            file_name = vault_info['name']

            file_type = magic.from_file(file_path, mime=True)

            if (file_type not in SUPPORTED_FILES):
                return (phantom.APP_SUCCESS)

            self._extract_file(action_result, file_path, file_name, recursive, container_id)

        return (phantom.APP_SUCCESS)

    def _extract_file(self, action_result, file_path, file_name, recursive, container_id=None,):

        if (container_id is None):
            container_id = self.get_container_id()

        file_type = magic.from_file(file_path, mime=True)

        if (file_type not in SUPPORTED_FILES):
            return action_result.set_status(phantom.APP_ERROR, "Deflation of file type: {0} not supported".format(file_type))

        if (file_type == 'application/zip'):
            if (not zipfile.is_zipfile(file_path)):
                return action_result.set_status(phantom.APP_ERROR, "Unable to deflate zip file")

            with zipfile.ZipFile(file_path, 'r') as vault_file:

                for compressed_file in vault_file.namelist():

                    save_as = os.path.basename(compressed_file)

                    if not os.path.basename(save_as):
                        continue

                    ret_val = self._add_file_to_vault(action_result, vault_file.read(compressed_file), save_as, recursive, container_id)

                    if phantom.is_fail(ret_val):
                        return action_result.set_status(phantom.APP_ERROR, "Error decompressing zip file.")

            return (phantom.APP_SUCCESS)

        # a tgz is also a tar file, so first extract it and add it to the vault
        if (tarfile.is_tarfile(file_path)):
            with tarfile.open(file_path, 'r') as vault_file:

                for member in vault_file.getmembers():

                    # Only interested in files, pass on dirs, links, etc.
                    if not member.isfile():
                        continue

                    ret_val = self._add_file_to_vault(action_result, vault_file.extractfile(member).read(), member.name, recursive, container_id)

                    if phantom.is_fail(ret_val):
                        return action_result.set_status(phantom.APP_ERROR, "Error decompressing tar file.")

            return (phantom.APP_SUCCESS)

        data = None
        if (file_type == 'application/x-bzip2'):
            # gz and bz2 don't provide a nice way to test, so trial and error
            try:
                with bz2.BZ2File(file_path, 'r') as f:
                    data = f.read()
            except IOError:
                return action_result.set_status(phantom.APP_ERROR, "Unable to deflate bz2 file")

        if (file_type == 'application/x-gzip'):
            try:
                with gzip.GzipFile(file_path, 'r') as f:
                    data = f.read()
            except IOError:
                return action_result.set_status(phantom.APP_ERROR, "Unable to deflate bz2 file")

        if data is None:
            return phantom.APP_SUCCESS

        ret_val = self._add_file_to_vault(action_result, data, os.path.splitext(file_name)[0], recursive, container_id)

        if phantom.is_fail(ret_val):
            return action_result.set_status(phantom.APP_ERROR, "Error decompressing {0} file. Details: {1}".format(file_type, action_result.get_message()))

        return action_result.set_status(phantom.APP_SUCCESS)

    def _deflate_item(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))

        vault_id = param['vault_id']

        try:
            vault_info = Vault.get_file_info(vault_id=vault_id)
            file_path = vault_info[0]['path']
            file_name = vault_info[0]['name']
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, "Failed to get vault item info", e)

        file_type = magic.from_file(file_path, mime=True)

        if (file_type not in SUPPORTED_FILES):
            return action_result.set_status(phantom.APP_ERROR, "Deflation of file type: {0} not supported".format(file_type))

        ret_val = self._extract_file(action_result, file_path, file_name, param.get('recursive', False), param.get('container_id'))

        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        action_result.set_summary({'total_vault_items': action_result.get_data_size()})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _find_listitem(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))

        values = param.get('values')
        list_name = param.get('list')
        exact_match = param.get('exact_match')
        column_index = int(param.get('column_index', -1))
        if column_index == '':
            column_index = -1

        endpoint = '/rest/decided_list/{}'.format(list_name)

        ret_val, response, resp_data = self._make_rest_call(endpoint, action_result)

        if phantom.is_fail(ret_val):
            return action_result.get_status()

        j = resp_data
        list_id = j['id']
        content = j.get('content')  # pylint: disable=E1101
        coordinates = []
        found = 0
        for rownum, row in enumerate(content):
            for cid, value in enumerate(row):
                if column_index < 0 or cid == column_index:
                    if exact_match and value == values:
                        found += 1
                        action_result.add_data(row)
                        coordinates.append((rownum, cid))
                    elif value and values in value:
                        found += 1
                        action_result.add_data(row)
                        coordinates.append((rownum, cid))

        action_result.update_summary({'server': self._base_uri, 'found matches': found, 'locations': coordinates, 'list_id': list_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _create_list(self, list_name, row, action_result):

        if type(row) in (str, unicode):
            row = [row]

        payload = {
            'content': [row],
            'name': list_name,
        }

        ret_val, response, resp_data = self._make_rest_call('/rest/decided_list', action_result, method='post', data=payload)

        if phantom.is_fail(ret_val):
            return action_result.get_status()

        action_result.add_data(resp_data)

        action_result.update_summary({'server': self._base_uri})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _add_listitem(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))

        list_name = param.get('list')

        row = param.get('new_row')

        try:
            row = ast.literal_eval(row)
        except:
            # it's just a string
            pass

        url = '/rest/decided_list/{}'.format(list_name)

        payload = {
            'append_rows': [
                row,
            ]
        }

        ret_val, response, resp_data = self._make_rest_call(url, action_result, method='post', data=payload)

        if phantom.is_fail(ret_val):
            if response.status_code == 404:
                if param.get('create'):
                    self.save_progress('List "{}" not found, creating'.format(list_name))
                    return self._create_list(list_name, row, action_result)
            return action_result.set_status(phantom.APP_ERROR, 'Error appending to list: {0}'.format(action_result.get_message()))

        action_result.add_data(resp_data)
        action_result.update_summary({'server': self._base_uri})

        return action_result.set_status(phantom.APP_SUCCESS)

    def initialize(self):

        # Validate that it is not localhost or 127.0.0.1,
        # this needs to be done just once, so do it here instead of handle_action,
        # since handle_action gets called for every item in the parameters list

        config = self.get_config()

        host = config['phantom_server']

        if (ph_utils.is_ip(host)):
            try:
                packed = socket.inet_aton(host)
                unpacked = socket.inet_ntoa(packed)
            except Exception as e:
                return self.set_status(phantom.APP_ERROR, "Unable to do ip to name conversion on {0}".format(host), e)
        else:
            try:
                unpacked = socket.gethostbyname(host)
            except:
                return self.set_status(phantom.APP_ERROR, "Unable to do name to ip conversion on {0}".format(host))

        if unpacked.startswith('127.'):
            return self.set_status(phantom.APP_ERROR, 'Accessing 127.0.0.1 is not allowed')

        if '127.0.0.1' in host or 'localhost' in host:
            return self.set_status(phantom.APP_ERROR, 'Accessing 127.0.0.1 is not allowed')

        self._base_uri = 'https://{}'.format(config['phantom_server'])
        self._verify_cert = config.get('verify_certificate', False)

        self._auth = None

        if config.get('username') and config.get('password'):
            self._auth = (config['username'], config['password'])

        return (phantom.APP_SUCCESS)

    def handle_action(self, param):
        """Function that handles all the actions

        Args:

        Return:
            A status code
        """

        result = None
        action = self.get_action_identifier()

        if (action == 'find_artifacts'):
            result = self._find_artifacts(param)
        elif (action == 'add_artifact'):
            result = self._add_artifact(param)
        elif (action == 'add_listitem'):
            result = self._add_listitem(param)
        elif (action == 'find_listitem'):
            result = self._find_listitem(param)
        elif (action == 'deflate_item'):
            result = self._deflate_item(param)
        elif (action == 'test_asset_connectivity'):
            result = self._test_connectivity(param)

        return result


if __name__ == '__main__':

    import sys
    # import simplejson as json
    import pudb

    pudb.set_trace()

    with open(sys.argv[1]) as f:
        in_json = f.read()
        in_json = json.loads(in_json)
        print(json.dumps(in_json, indent=4))

        connector = PhantomConnector()
        connector.print_progress_message = True
        ret_val = connector._handle_action(json.dumps(in_json), None)
        print json.dumps(json.loads(ret_val), indent=4)

    exit(0)