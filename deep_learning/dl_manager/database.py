##############################################################################
##############################################################################
# Imports
##############################################################################

import collections
import json
import warnings

import requests
import typing

from .config import conf
from .logger import get_logger

log = get_logger('Database Manager')

##############################################################################
##############################################################################
# Query Parsing
##############################################################################

def parse_query(q):
    try:
        return json.loads(q)
    except json.JSONDecodeError as e:
        raise ValueError(f'Failed to parse {q}') from e

##############################################################################
##############################################################################
# Query Validation
##############################################################################

def validate_query(query, *, __force_eq=False):
    if not isinstance(query, dict):
        raise _invalid_query(query)
    if len(query) != 1:
        raise _invalid_query(query, 'expected exactly 1 element')
    match query:
        case {'$and': operands}:
            if __force_eq:
                raise _invalid_query(query, '$and was not expected here')
            if not isinstance(operands, list):
                raise _invalid_query(query, '$and operand must be a list')
            for o in operands:
                validate_query(o)
        case {'$or': operands}:
            if __force_eq:
                raise _invalid_query(query, '$or was not expected here')
            if not isinstance(operands, list):
                raise _invalid_query(query, '$or operand must be a list')
            for o in operands:
                validate_query(o)
        case {'tags': operand}:
            if not isinstance(operand, dict):
                raise _invalid_query('tag operand must be an object')
            validate_query(operand, __force_eq=True)
        case {'project': operand}:
            if not isinstance(operand, dict):
                raise _invalid_query('project operand must be an object')
            validate_query(operand, __force_eq=True)
        case {'$eq': operand}:
            if not __force_eq:
                raise _invalid_query(query, '$eq not expected here')
            if not isinstance(operand, str):
                raise _invalid_query(query, '$eq operand must be a string')
        case {'$neq': operand}:
            if not __force_eq:
                raise _invalid_query(query, '$neq not expected here')
            if not isinstance(operand, str):
                raise _invalid_query(query, '$neq operand must be a string')
        case _ as x:
            raise _invalid_query(x, 'Invalid operation')


def _invalid_query(q, msg=None):
    if msg is not None:
        return ValueError(f'Invalid (sub-)query ({msg}): {q}')
    return ValueError(f'Invalid (sub-)query: {q}')


##############################################################################
##############################################################################
# Requests
##############################################################################


def _call_endpoint(endpoint, payload, verb):
    url = f'{conf.get("system.storage.database-url")}/{endpoint}'
    log.info(f'Calling endpoint {endpoint}')
    log.debug(f'Request payload: {payload}')
    match verb:
        case 'GET':
            response = requests.get(url, json=payload)
            response_payload = response.json()
            log.debug(f'Response payload: {response_payload}')
            return response_payload
        case 'POST':
            requests.post(url, json=payload)
        case _ as x:
            raise ValueError(f'Invalid verb: {x}')


def select_issue_ids(query) -> list[str]:
    parsed = parse_query(query)
    validate_query(parsed)
    return _call_endpoint('issue-ids', {'filter': parsed}, 'GET')['ids']


def get_issue_labels_by_key(ids: list[str]):
    return _call_endpoint('manual-labels', {'ids': ids}, 'GET')['labels']


def get_issue_data_by_keys(ids: list[str], attributes: list[str]):
    return _call_endpoint(
        'issue-data',
        {
            'ids': ids,
            'attributes': attributes
        },
        'GET'
    )['data']


def add_tag_to_issues(ids: list[str], tag: str):
    return _call_endpoint(
        'add-tags',
        {
            'ids': ids,
            'tags': [tag]
        },
        'POST'
    )


def save_predictions(model_name: str,
                     predictions_by_id: dict[str, dict[str, typing.Any]]):
    _call_endpoint(
        'save-predictions',
        {
            'model-id': model_name,
            'predictions': predictions_by_id
        },
        'POST'
    )


##############################################################################
##############################################################################
# High level DB interface
##############################################################################


class DatabaseAPI:

    def __init__(self):
        self.__cache = collections.defaultdict(dict)

    def select_issues(self, query):
        key = json.dumps(query)
        if key not in self.__cache['select-issue-ids']:
            self.__cache['select-issue-ids'][key] = select_issue_ids(query)
        return self.__cache['select-issue-ids'][key]

    def get_labels(self, ids: list[str]):
        local_cache = self.__cache['issue-labels']
        cached_keys = {key
                       for key in ids
                       if key in local_cache}
        required_keys = [key for key in ids if key not in cached_keys]
        if required_keys:
            labels = get_issue_labels_by_key(required_keys)
            local_cache.update(labels)
        return [local_cache[key] for key in ids]

    def get_issue_data(self,
                       issue_ids: list[str],
                       attributes: list[str],
                       *,
                       raise_on_partial_result=False):
        local_cache = self.__cache['issue-data']
        attrs = set(attributes)
        required_keys = []
        for key in issue_ids:
            if key not in local_cache:
                required_keys.append(key)
            elif not (attrs.issubset(local_cache['key'])):
                if raise_on_partial_result:
                    raise ValueError(f'Partially loaded set of attributes for key {key}')
                required_keys.append(key)
        if required_keys:
            data = get_issue_data_by_keys(required_keys, attributes)
            local_cache.update(data)
        wrong = [key for key in issue_ids if key not in local_cache]
        warnings.warn('Remember to remove `wrong` array once database has been updated!')
        return [local_cache[key] for key in issue_ids if key not in wrong]

    def add_tag(self, ids: list[str], tag: str):
        add_tag_to_issues(ids, tag)

    def save_predictions(self,
                         model_name: str,
                         predictions_by_id: dict[str, dict[str, typing.Any]]):
        save_predictions(model_name, predictions_by_id)
