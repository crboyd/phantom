# File: phantom_connector.py
# Copyright (c) 2016-2020 Splunk Inc.
#
# SPLUNK CONFIDENTIAL - Use or disclosure of this material in whole or in part
# without a valid written license from Splunk Inc. is PROHIBITED.
# --

# Phantom imports
import phantom.app as phantom

from phantom.base_connector import BaseConnector
from phantom.action_result import ActionResult

from phantom.cef import CEF_NAME_MAPPING
from phantom.cef import CEF_JSON
from phantom.utils import CONTAINS_VALIDATORS
import phantom.utils as ph_utils
from phantom.vault import Vault

import ast
import json
import requests
import unicodedata
from requests.exceptions import Timeout, SSLError

import socket
from bs4 import BeautifulSoup
import os
import zipfile
import magic
import tarfile
import gzip
import bz2
import datetime
import time
import urllib
import random
import string

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

        if type(resp_json) == list:
            # Let's not parse it here
            return RetVal3(phantom.APP_SUCCESS, response, resp_json)

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
        message = "Can't process response from server. Status Code: {0} Data from server: {1}".format(
                response.status_code, response.text.replace('{', ' ').replace('}', ' '))

        return RetVal3(action_result.set_status(phantom.APP_ERROR, message), response, None)

    def _make_rest_call(self, endpoint, action_result, headers=None, params=None, data=None, method="get", ignore_auth=False):

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

        auth = self._auth

        if (ignore_auth):
            auth = None
            if ('ph-auth-token' in headers):
                del headers['ph-auth-token']

        try:
            response = request_func(self._base_uri + endpoint,
                    auth=auth,
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
            self.save_progress("Test Connectivity Failed")
            return self.set_status(phantom.APP_ERROR, 'Failed to connect: {}'.format(action_result.get_message()))

        version = resp_data['version']
        self.save_progress("Connected to Phantom appliance version {}".format(version))
        self.save_progress("Test connectivity passed")
        return self.set_status(phantom.APP_SUCCESS, 'Request succeeded')

    def load_dirty_json(self, dirty_json):
        import re
        regex_replace = [
            (r"([ \{,:\[])(u?\\?)?'([^']*)'([^'])", r'\1"\3"\4'),   # Replace single quotes with double quotes
            (r" False([, \}\]])", r' false\1'),                     # Replace python "False" with json "false"
            (r" True([, \}\]])", r' true\1'),                       # Replace python "True" with json "true"
            (r" None([, \}\]])", r' null\1')                        # Replace python "None" with json "null"
        ]
        for r, s in regex_replace:
            dirty_json = re.sub(r, s, dirty_json)
        dirty_json = dirty_json.replace(": ''", ': ""')
        clean_json = json.loads(dirty_json)

        return clean_json

    def _update_artifact(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))

        artifact_id = param['artifact_id']

        name = param.get('name')
        label = param.get('label')
        severity = param.get('severity')
        cef_json = param.get('cef_json')
        cef_types_json = param.get('cef_types_json')
        tags = param.get('tags')
        art_json = param.get('artifact_json')

        overwrite = param.get('overwrite', False)

        # Check if at least one of the following parameters have been supplied:
        if not any((name, label, severity, cef_json, cef_types_json, tags, art_json)):
            req_params = 'name, label, severity, cef_json, cef_types_json, tags, artifact_json'
            return action_result.set_status(phantom.APP_ERROR, 'At least one of the following parameters are required to update an artifact: {}'.format(req_params))

        endpoint = "/rest/artifact/{}".format(artifact_id)

        output_artifact = {}

        # name, label, and severity should always be overwritten, if provided
        if name:
            output_artifact['name'] = name

        if label:
            output_artifact['label'] = label

        if severity:
            output_artifact['severity'] = severity

        existing_artifact = {}  # If overwriting, this will be used.

        # //// Start workaround for PPS-18970 ////
        ''' Use this once PPS-18970 is fixed
        if overwrite is False:
            # Get the existing artifact to append provided parameters to existing values
            ret_val, response, resp_data = self._make_rest_call(endpoint, action_result)

            if phantom.is_fail(ret_val):
                self.save_progress('Unable to find artifact, please check the artifact id.')
                return action_result.set_status(phantom.APP_ERROR, 'Failed to get artifact: {}'.format(action_result.get_message()))

            existing_artifact = resp_data
        '''

        ret_val, response, resp_data = self._make_rest_call(endpoint, action_result)

        if phantom.is_fail(ret_val):
            self.save_progress('Unable to find artifact, please check the artifact id.')
            return action_result.set_status(phantom.APP_ERROR, 'Failed to get artifact: {}'.format(action_result.get_message()))

        if overwrite is False:
            existing_artifact = resp_data
        if 'label' not in output_artifact:
            output_artifact['label'] = resp_data.get('label')
            if not output_artifact['label']:
                output_artifact['label'] = 'event'

        # //// End workaround for PPS-18970 ////

        if cef_json:
            # If overwrite is False, need to update existing CEF verses replacing whole thing
            cef = existing_artifact.get('cef', {})
            cef.update(self.load_dirty_json(cef_json))
            output_artifact['cef'] = cef

        if cef_types_json:
            # If overwrite is False, need to update existing cef_types verses replacing whole thing
            contains = existing_artifact.get('cef_types', {})
            contains.update(self.load_dirty_json(cef_types_json))
            output_artifact['cef_types'] = contains

        if tags:
            # If overwrite is False, need to add to the existing tags. Otherwise replace list of tags.
            cleaned_tags = [tag.strip().strip('\'"') for tag in tags.strip('[]').split(',')]
            output_artifact['tags'] = list(set(existing_artifact.get('tags', []) + cleaned_tags))  # make sure any duplicates are removed

        # This will always overwrite any existing fields provided.
        if art_json:
            output_artifact.update(self.load_dirty_json(art_json))

        ret_val, response, resp_data = self._make_rest_call(endpoint, action_result, data=output_artifact, method="post")

        action_result.add_data({
            'requested_artifact': output_artifact,
            'response': resp_data
        })

        if phantom.is_fail(ret_val):
            self.save_progress('Unable to update artifact.')
            return action_result.set_status(phantom.APP_ERROR, 'Failed to update artifact: {}'.format(action_result.get_message()))

        return action_result.set_status(phantom.APP_SUCCESS, 'Artifact updated successfully.')

    def _tag_artifact(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))

        artifact_id = param.get('artifact_id', '')
        add_tags = param.get('add_tags', '')
        remove_tags = param.get('remove_tags', '')

        # These come in as str, so split, then convert to set
        add_tags = set(add_tags.split(','))
        remove_tags = set(remove_tags.split(','))

        endpoint = "/rest/artifact/" + artifact_id
        # First get the artifacts json
        ret_val, response, resp_data = self._make_rest_call(endpoint, action_result)

        if (phantom.is_fail(ret_val)):
            self.save_progress("Unable to get artifact, please check the artifact id")
            return action_result.set_status(phantom.APP_ERROR, 'Failed to get artifact: {}'.format(action_result.get_message()))

        # Label has to be included or it gets clobbered in POST
        fields = ['tags', 'label']
        art_data = {f: response.json().get(f) for f in fields}

        # Set union first to add, then difference to remove, then cast back to list to update
        _tags = (set(art_data['tags']) | add_tags) - remove_tags
        art_data['tags'] = list(_tags)

        # Post our changes
        ret_val, response, resp_data = self._make_rest_call(endpoint, action_result, data=art_data, method="post")

        if (phantom.is_fail(ret_val)):
            self.save_progress("Unable to modify artifact")
            return action_result.set_status(phantom.APP_ERROR, 'Failed to update artifact: {}'.format(action_result.get_message()))
        return action_result.set_status(phantom.APP_SUCCESS, "Artifact Updated")

    def _add_note(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))

        endpoint = '/rest/container_note'

        note_data = {
            'container_id': param.get('container_id', self.get_container_id()),
            'title': param.get('title', ''),
            'content': param.get('content', ''),
            'phase_id': param.get('phase_id', None)
        }

        ret_val, response, resp_data = self._make_rest_call(endpoint, action_result, data=note_data, method="post")

        if phantom.is_fail(ret_val):
            self.save_progress('Unable to create note')
            return action_result.set_status(phantom.APP_ERROR, "Failed to create note: {}".format(action_result.get_message()))
        return action_result.set_status(phantom.APP_SUCCESS, "Note created")

    def _find_artifacts(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))
        limit_search = param.get("limit_search", False)
        container_ids = param.get("container_ids", "current")
        if limit_search:
            container_ids = list(
                set([
                    a for a in [
                        int(z) if isinstance(z, int) or z.isdigit() else None for z in [
                            self.get_container_id() if y == "current" else y for y in
                            [x.strip() for x in container_ids.replace(",", " ").split()]
                        ]
                    ] if a
                ])
            )
        action_result.update_param({"container_ids": str(sorted(container_ids)).strip("[]")})
        values = param.get('values', '')

        if param.get('is_regex'):
            flt = 'iregex'
        else:
            flt = 'icontains'

        exact_match = param.get('exact_match')

        if exact_match:
            values = '"{}"'.format(values)

        values = urllib.quote(values, safe=':/')

        endpoint = '/rest/artifact?_filter_cef__{}={}&page_size=0&pretty'.format(flt, repr(values))

        ret_val, response, resp_data = self._make_rest_call(endpoint, action_result)

        if phantom.is_fail(ret_val):
            return action_result.set_status(phantom.APP_ERROR, 'Error retrieving records: {0}'.format(action_result.get_message()))

        if limit_search:
            records = [x for x in resp_data['data'] if int(x['container']) in container_ids]
        else:
            records = resp_data['data']

        values = values.lower()

        for rec in records:
            key, value = None, None

            for k, v in rec['cef'].iteritems():

                curr_value = v

                if ( isinstance(curr_value, dict)):
                    curr_value = json.dumps(curr_value)

                if (not isinstance(curr_value, basestring)):
                    curr_value = str(curr_value)

                if values in curr_value.lower() or (exact_match and values.strip('"') == curr_value.lower()):
                    key = k
                    value = curr_value
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
        sdi = param.get('source_data_identifier')
        label = param.get('label', 'event')
        contains = param.get('contains')
        cef_name = param.get('cef_name')
        cef_value = param.get('cef_value')
        cef_dict = param.get('cef_dictionary')
        run_automation = param.get('run_automation', "true")

        loaded_cef = {}
        loaded_contains = {}

        if cef_dict:

            try:
                loaded_cef = json.loads(cef_dict)
            except Exception as e:
                return action_result.set_status(phantom.APP_ERROR, "Could not load JSON from CEF paramter", e)

            try:
                loaded_contains = json.loads(contains)
            except Exception as e:
                return action_result.set_status(phantom.APP_ERROR, "Could not load JSON from contains paramter", e)

        if cef_name and cef_value:
            loaded_cef[cef_name] = cef_value
            loaded_contains[cef_name] = [contains]

        artifact = {}
        artifact['name'] = name
        artifact['label'] = label
        artifact['container_id'] = container_id
        artifact['cef'] = loaded_cef
        artifact['cef_types'] = loaded_contains
        artifact['source_data_identifier'] = sdi
        artifact['run_automation'] = run_automation

        for cef_name in loaded_cef:

            if loaded_contains.get(cef_name):
                continue

            if cef_name not in CEF_NAME_MAPPING:
                determined_contains = determine_contains(loaded_cef[cef_name])
                if determined_contains:
                    artifact['cef_types'][cef_name] = [determined_contains]
            else:
                try:
                    artifact['cef_types'][cef_name] = CEF_JSON[cef_name]['contains']
                except:
                    pass

        success, response, resp_data = self._make_rest_call('/rest/artifact', action_result, method='post', data=artifact)

        if not resp_data:
            return action_result.get_status()

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

        try:
            file_name = file_name.decode('utf-8', 'replace')
        except UnicodeEncodeError:
            file_name = unicodedata.normalize('NFKD', file_name).encode('utf-8', 'ignore')

        save_as = file_name or '_invalid_file_name_'

        # PAPP-9543 append a random string to the filename to make concurrent action runs succeed
        save_as = save_as + '_' + ''.join(random.SystemRandom().choice(string.ascii_lowercase) for _ in range(16))

        # if the path contains a directory
        if (os.path.dirname(save_as)):
            save_as = '-'.join(save_as.split(os.sep))

        if hasattr(Vault, 'get_vault_tmp_dir'):
            vault_tmp_dir = Vault.get_vault_tmp_dir()
        else:
            vault_tmp_dir = '/opt/phantom/vault/tmp'

        save_path = os.path.join(vault_tmp_dir, save_as)
        with open(save_path, 'w') as uncompressed_file:
            uncompressed_file.write(data_stream)

        try:
            vault_info = Vault.add_attachment(save_path, container_id, file_name)
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, "Failed to add file into vault", e)

        if not vault_info.get('succeeded', False):
            return action_result.set_status(phantom.APP_ERROR, "Failed to add file into vault, {0}".format(vault_info.get('message', 'NA')))

        try:
            query_params = {
                '_filter_vault_document__hash': '"{}"'.format(vault_info['vault_id'].lower()),
                'page_size': 1,
                'pretty': ''
            }
            ret_val, response, resp_data = self._make_rest_call('/rest/container_attachment', action_result, params=query_params)
            vault_info = resp_data['data'][0]
            for k in vault_info.keys():
                if k.startswith('_pretty_'):
                    name = k[8:]
                    vault_info[name] = vault_info.pop(k)
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, "Failed to retrieve info about file added to vault", e)

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
                        return ret_val

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
            query_params = {
                '_filter_vault_document__hash': '"{}"'.format(vault_id),
                'page_size': 1,
                'pretty': ''
            }
            ret_val, response, resp_data = self._make_rest_call('/rest/container_attachment', action_result, params=query_params)
            vault_info = resp_data['data'][0]
            for k in vault_info.keys():
                if k.startswith('_pretty_'):
                    name = k[8:]
                    vault_info[name] = vault_info.pop(k)
            file_path = vault_info['path']
            file_name = vault_info['name']
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

    def _add_artifact_list(self, action_result, artifacts, ignore_auth=False):
        """ Add a list of artifacts """
        ret_val, response, resp_data = self._make_rest_call('/rest/artifact', action_result, method='post', data=artifacts)
        if phantom.is_fail(ret_val):
            return action_result.set_status(phantom.APP_ERROR, "Error adding artifact: {}".format(action_result.get_message()))
        failed = 0
        for resp in resp_data:  # is a list
            if resp.get('failed') is True:
                self.debug_print(resp.get('message'))
                failed += 1
        if failed:
            action_result.update_summary({'failed_artifact_count': failed})
            return action_result.set_status(phantom.APP_ERROR, "Failed to add one or more artifacts")
        return phantom.APP_SUCCESS

    def _create_container_copy(self, action_result, container_id, destination, source, source_local=False, destination_local=False):
        """ destination: where new container is being made """
        """ source: where the original container is """
        """ Create a copy of this existing container, including all of its artifacts """

        # Retrieve original container
        self._base_uri = source
        url = '/rest/container/{}'.format(container_id)

        ret_val, response, resp_data = self._make_rest_call(url, action_result)

        if phantom.is_fail(ret_val):
            return ret_val

        container = resp_data
        # Remove data from original we dont want
        container.pop('asset', None)
        container.pop('artifact_count', None)
        container.pop('start_time', None)
        container.pop('source_data_identifier', None)
        container.pop('ingest_app')
        container.pop('tenant')
        container.pop('id')
        container['owner_id'] = container.pop('owner')
        if (destination_local):
            container['asset_id'] = int(self.get_asset_id())
        # container['ingest_app_id'] = container.pop('ingest_app', None)

        self._base_uri = destination
        ret_val, response, resp_data = self._make_rest_call('/rest/container', action_result, method='post', data=container)
        if phantom.is_fail(ret_val):
            act_message = action_result.get_message()
            if ('ingesting asset_id' in act_message):
                act_message += 'If Multi-tenancy is enabled, please make sure the asset is assigned a tenant'
                action_result.set_status(ret_val, act_message)
            return ret_val

        try:
            new_container_id = resp_data['id']
        except KeyError:
            # The newly created container wont get cleaned up
            return action_result.set_status(phantom.APP_ERROR, "Unable to retrieve ID of newly created container")

        # Retrieve artifacts from old container
        url = '/rest/container/{}/artifacts'.format(container_id)
        params = {'sort': 'id', 'order': 'asc', 'page_size': 0}
        self._base_uri = source
        ret_val, response, resp_data = self._make_rest_call(url, action_result, params=params)

        artifacts = resp_data['data']
        if artifacts:
            for artifact in artifacts:
                # Remove data from artifacts that we dont want
                artifact.pop('update_time', None)
                artifact.pop('create_time', None)
                artifact.pop('start_time', None)
                artifact.pop('end_time', None)
                artifact.pop('asset_id', None)
                artifact.pop('container', None)
                artifact.pop('id', None)
                artifact['run_automation'] = False
                artifact['container_id'] = new_container_id
                artifact['owner_id'] = artifact.pop('owner')
            artifacts[-1]['run_automation'] = True

            self._base_uri = destination
            ret_val = self._add_artifact_list(action_result, artifacts)
            if phantom.is_fail(ret_val):
                return ret_val

        action_result.update_summary({'container_id': new_container_id, 'artifact_count': len(artifacts)})
        return action_result.set_status(phantom.APP_SUCCESS)

    def _create_container_new(self, action_result, container_json, artifact_json_list):
        try:
            container = json.loads(container_json)
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, "Error parsing container JSON: {}".format(str(e)))

        if artifact_json_list:
            try:
                artifacts = json.loads(artifact_json_list)
            except Exception as e:
                return action_result.set_status(phantom.APP_ERROR, "Error parsing artifacts list JSON: {}".format(str(e)))
        else:
            artifacts = []

        ret_val, response, resp_data = self._make_rest_call('/rest/container', action_result, method='post', data=container)
        if phantom.is_fail(ret_val):
            return ret_val

        try:
            new_container_id = resp_data['id']
        except KeyError:
            # The newly created container wont get cleaned up
            return action_result.set_status(phantom.APP_ERROR, "Unable to retrieve ID of newly created container")

        if artifacts:
            for artifact in artifacts:
                artifact['run_automation'] = False
                artifact['container_id'] = new_container_id
            artifacts[-1]['run_automation'] = True

            ret_val = self._add_artifact_list(action_result, artifacts)
            if phantom.is_fail(ret_val):
                return ret_val

        action_result.update_summary({'container_id': new_container_id, 'artifact_count': len(artifacts)})
        return action_result.set_status(phantom.APP_SUCCESS)

    def _create_container(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))

        container_json = param['container_json']
        container_artifacts = param.get('container_artifacts')
        return self._create_container_new(action_result, container_json, container_artifacts)

    def _export_container(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))

        container_id = param['container_id']

        destination = self._base_uri
        source = self.get_phantom_base_url()

        return self._create_container_copy(action_result, container_id, destination, source, source_local=True)

    def _import_container(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))

        container_id = param['container_id']

        destination = self.get_phantom_base_url()
        source = self._base_uri

        return self._create_container_copy(action_result, container_id, destination, source, destination_local=True)

    def _get_action(self, param):

        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))
        action_result = self.add_action_result(ActionResult(dict(param)))

        url_params = {
                '_filter_action': '"{0}"'.format(param['action_name']),
                'include_expensive': '',
                'sort': 'start_time',
                'order': 'desc',
            }

        parameters = {}
        if 'parameters' in param:

            try:
                parameters = json.loads(param['parameters'])
            except:
                return action_result.set_status(phantom.APP_ERROR, "Could not load JSON from 'parameters' parameter")

            search_key, search_value = parameters.popitem()
            url_params['_filter_result_data__regex'] = '"parameter.*\\"{0}\\": \\"{1}\\""'.format(search_key, search_value)

        if 'time_limit' in param:
            hours = int(param['time_limit'])
            time_str = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            url_params['_filter_start_time__gt'] = '"{0}"'.format(time_str)

        if 'max_results' in param:
            limit = int(param['max_results'])
            url_params['page_size'] = limit

        if 'app' in param:

            app_params = {'_filter_name__iexact': '"{0}"'.format(param['app'])}
            ret_val, response, resp_json = self._make_rest_call('/rest/app', action_result, params=app_params)

            if phantom.is_fail(ret_val):
                return ret_val

            if resp_json['count'] == 0:
                return action_result.set_status(phantom.APP_ERROR, "Could not find app with name '{0}'".format(param['app']))

            url_params['_filter_app'] = resp_json['data'][0]['id']

        if 'asset' in param:

            asset_params = {'_filter_name__iexact': '"{0}"'.format(param['asset'])}
            ret_val, response, resp_json = self._make_rest_call('/rest/asset', action_result, params=asset_params)

            if phantom.is_fail(ret_val):
                return ret_val

            if resp_json['count'] == 0:
                return action_result.set_status(phantom.APP_ERROR, "Could not find asset with name '{0}'".format(param['asset']))

            url_params['_filter_asset'] = resp_json['data'][0]['id']

        ret_val, response, resp_json = self._make_rest_call('/rest/app_run', action_result, params=url_params)

        if phantom.is_fail(ret_val):
            return ret_val

        count = 0
        if len(parameters) > 0:

            for action_run in resp_json['data']:

                for result in action_run['result_data']:

                    cur_params = result['parameter']

                    found = True
                    for k, v in parameters.iteritems():

                        if cur_params.get(k) != v:
                            found = False
                            break

                    if found:
                        count += 1
                        action_result.add_data(action_run)
                        return action_result.set_status(phantom.APP_SUCCESS)

            return action_result.set_status(phantom.APP_SUCCESS, "No action results found matching given criteria")

            action_result.set_summary({'num_results': count})
            return action_result.set_status(phantom.APP_SUCCESS)

        elif resp_json['count'] == 0:
            return action_result.set_status(phantom.APP_SUCCESS, "No action results found matching given criteria")

        for action_run in resp_json['data']:
            action_result.add_data(action_run)

        action_result.set_summary({'num_results': len(resp_json['data'])})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _update_list(self, param):

        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))
        action_result = self.add_action_result(ActionResult(dict(param)))

        row_number = str(param['row_number'])
        row_values_as_list = param['row_values_as_list']

        list_identifier = param.get('list_name')
        if not list_identifier:
            list_identifier = param.get('id')
        if not list_identifier:
            return action_result.set_status(phantom.APP_ERROR, "Either the custom list's name or id must be provided")

        row_values = [v.strip() for v in row_values_as_list.split(",")]

        data = {
            "update_rows": {
                row_number: row_values
            }
        }

        # make rest call
        ret_val, response, resp_data = self._make_rest_call('/rest/decided_list/{}'.format(list_identifier), action_result, data=data, method="post")

        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        # Add the response into the data section
        action_result.add_data(resp_data)

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary['success'] = True

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        return action_result.set_status(phantom.APP_SUCCESS)

    def _no_op(self, param):

        action_result = self.add_action_result(ActionResult(dict(param)))

        sleep_seconds = param['sleep_seconds']

        try:
            sleep_seconds = int(sleep_seconds)
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, "Error parsing the sleep seconds parameter. Reason: {0}".format(str(e)))

        if (sleep_seconds < 0):
            return action_result.set_status(phantom.APP_ERROR, "Invalid sleep_seconds value. Please specify a value greater or equal to 0")

        remainder = sleep_seconds % 60

        self.send_progress("Sleeping...")
        for i in range(0, int(sleep_seconds / 60)):
            time.sleep(60)
            self.send_progress("Slept for {} minute{}...", i + 1, 's' if i else '')

        if remainder:
            time.sleep(remainder)

        return action_result.set_status(phantom.APP_SUCCESS, "Slept for {} seconds".format(sleep_seconds))

    def initialize(self):

        # Validate that it is not localhost or 127.0.0.1,
        # this needs to be done just once, so do it here instead of handle_action,
        # since handle_action gets called for every item in the parameters list

        config = self.get_config()

        host = config['phantom_server']
        # Split hostname from port
        host = host.split(':')[0]

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
            return self.set_status(phantom.APP_ERROR,
                    'Accessing 127.0.0.1 is not allowed. Please specify the actual IP or hostname used by the Phantom instance in the Asset config')

        if '127.0.0.1' in host or 'localhost' in host:
            return self.set_status(phantom.APP_ERROR,
                    'Accessing 127.0.0.1 is not allowed. Please specify the actual IP or hostname used by the Phantom instance in the Asset config')

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
        elif (action == 'create_container'):
            result = self._create_container(param)
        elif (action == 'export_container'):
            result = self._export_container(param)
        elif (action == 'import_container'):
            result = self._import_container(param)
        elif (action == 'get_action'):
            result = self._get_action(param)
        elif (action == 'update_list'):
            result = self._update_list(param)
        elif (action == 'no_op'):
            return self._no_op(param)
        elif (action == "update_artifact"):
            return self._update_artifact(param)
        elif (action == "add_note"):
            return self._add_note(param)
        elif (action == "tag_artifact"):
            return self._tag_artifact(param)

        return result


if __name__ == '__main__':

    import pudb
    import argparse

    pudb.set_trace()

    argparser = argparse.ArgumentParser()

    argparser.add_argument('input_test_json', help='Input Test JSON file')
    argparser.add_argument('-u', '--username', help='username', required=False)
    argparser.add_argument('-p', '--password', help='password', required=False)

    args = argparser.parse_args()
    session_id = None

    username = args.username
    password = args.password

    if (username is not None and password is None):

        # User specified a username but not a password, so ask
        import getpass
        password = getpass.getpass("Password: ")

    if (username and password):
        try:
            print ("Accessing the Login page")
            r = requests.get(BaseConnector._get_phantom_base_url() + "login", verify=False)
            csrftoken = r.cookies['csrftoken']

            data = dict()
            data['username'] = username
            data['password'] = password
            data['csrfmiddlewaretoken'] = csrftoken

            headers = dict()
            headers['Cookie'] = 'csrftoken=' + csrftoken
            headers['Referer'] = BaseConnector._get_phantom_base_url() + 'login'

            print ("Logging into Platform to get the session id")
            r2 = requests.post(BaseConnector._get_phantom_base_url() + "login", verify=False, data=data, headers=headers)
            session_id = r2.cookies['sessionid']
        except Exception as e:
            print ("Unable to get session id from the platfrom. Error: " + str(e))
            exit(1)

    with open(args.input_test_json) as f:
        in_json = f.read()
        in_json = json.loads(in_json)
        print(json.dumps(in_json, indent=4))

        connector = PhantomConnector()
        connector.print_progress_message = True

        if (session_id is not None):
            in_json['user_session_token'] = session_id
            connector._set_csrf_info(csrftoken, headers['Referer'])

        ret_val = connector._handle_action(json.dumps(in_json), None)
        print (json.dumps(json.loads(ret_val), indent=4))

    exit(0)
