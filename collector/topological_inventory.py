import logging
import os
from threading import current_thread
from collections import defaultdict

import base64
import json
import yaml
from objsize import get_deep_size

import prometheus_metrics
from . import utils
from .env import (APP_NAME, TENANTS_URL, ALL_TENANTS,
                  SOURCES_HOST, SOURCES_PATH,
                  TOPOLOGICAL_INVENTORY_HOST, TOPOLOGICAL_INVENTORY_PATH)

LOGGER = logging.getLogger()
CFG_DIR = '{}/configs'.format(os.path.dirname(__file__))

# Provide mapping for all available services, default to TOPOLOGICAL
SERVICES_URL = defaultdict(
    lambda: dict(
        host=TOPOLOGICAL_INVENTORY_HOST,
        path=TOPOLOGICAL_INVENTORY_PATH
    ),
    SOURCES=dict(
        host=SOURCES_HOST,
        path=SOURCES_PATH
    )
)


DATA_COLLECTION_TIME = prometheus_metrics.METRICS['data_collection_time']


def _load_yaml(filename: str) -> dict:
    """Yaml filename loader helper.

    Parameters
    ----------
    filename (str)
        Yaml file to load

    Returns
    -------
    dict
        YAML content as Pythonic dict

    """
    with open(filename) as yaml_file:
        return yaml.full_load(yaml_file)


APP_CONFIG = _load_yaml(f'{CFG_DIR}/topological_app_config.yml').get(APP_NAME)
QUERIES = _load_yaml(f'{CFG_DIR}/topological_queries.yml')


def _update_fk(page_data: list, fk_name: str, fk_id: str) -> dict:
    """Mutate Rows with Foreign key info.

    Updates foreign key values for each entry in given page_data

    Parameters
    ----------
    page_data (list)
        Data on a page which should be modified
    fk_name (str)
        Column where the Foreign Key is located
    fk_id (str)
        Foreign Key value

    Returns
    -------
    list
        Updated page_data

    """
    if not (fk_name and fk_id):
        return page_data

    for row in page_data:
        row[fk_name] = fk_id
    return page_data


def _collect_data(host: dict, url: str, fk_name: str = None,
                  fk_id: str = None, headers: dict = None) -> dict:
    """Aggregate data from all pages.

    Returns data aggregated from all pages together

    Parameters
    ----------
    host (str)
        Service host
    url (str)
        URI to the first page, where to start the traverse
    fk_name (str)
        Foreign key column to update if needed
    fk_id (str)
        Foreign key value to update if needed

    Returns
    -------
    dict
        All data aggregated across all pages

    Raises
    ------
    utils.RetryFailedError
        Connection failed, data is not complete

    """
    # Collect data from the first page
    url = f'{host["host"]}/{host["path"]}/{url}'
    prometheus_metrics.METRICS['gets'].inc()
    resp = utils.retryable('get', url, headers=headers)
    prometheus_metrics.METRICS['get_successes'].inc()
    resp = resp.json()
    all_data = resp['data']

    # Walk all pages
    while resp['links'].get('next'):
        prometheus_metrics.METRICS['gets'].inc()
        resp = utils.retryable(
            'get',
            f'{host["host"]}{resp["links"]["next"]}',
            headers=headers
        )
        resp = resp.json()
        prometheus_metrics.METRICS['get_successes'].inc()
        all_data += resp['data']

    return _update_fk(all_data, fk_name, fk_id)


def _query_main_collection(entity: dict, headers: dict = None) -> dict:
    """Query a Collection.

    Parameters
    ----------
    entity (dict)
        A query_spec entity to download

    Returns
    -------
    dict
        All data aggregated across all pages

    Raises
    ------
    utils.RetryFailedError
        Connection failed, data is not complete

    """
    collection = entity['main_collection']
    service = SERVICES_URL[entity.get('service')]

    return _collect_data(service, collection, headers=headers)


def _query_sub_collection(entity: dict, data: dict,
                          headers: dict = None) -> dict:
    """Query a SubCollection for all records in the main collection.

    Parameters
    ----------
    entity (dict)
        A query_spec entity to download
    data (dict)
        Already available data for reference
    foreign_key (str)
        Foreign key column to be populated in sub-collection records

    Returns
    -------
    dict
        All data aggregated across all pages

    Raises
    ------
    utils.RetryFailedError
        Connection failed, data is not complete

    """
    main_collection = entity['main_collection']
    sub_collection = entity['sub_collection']
    foreign_key = entity['foreign_key']
    service = SERVICES_URL[entity.get('service')]

    url = f'{main_collection}/{{}}/{sub_collection}'
    all_data = []
    for item in data[main_collection]:
        all_data += _collect_data(service, url.format(item['id']),
                                  foreign_key, item['id'], headers=headers)
    return all_data


@DATA_COLLECTION_TIME.time()
def worker(_: str, source_id: str, dest: str, acct_info: dict) -> None:
    """Worker for topological inventory.

    Parameters
    ----------
    _ (str)
        Skipped
    source_id (str)
        Job identifier
    dest (str)
        URL where to pass data
    acct_info (dict)
        contains e.g. Red Hat Identity base64 string and account_id

    """
    thread = current_thread()
    LOGGER.debug('%s: Worker started', thread.name)

    b64_identity = acct_info['b64_identity']
    account_id = acct_info['account_id']
    headers = {"x-rh-identity": b64_identity}

    if ALL_TENANTS:
        prometheus_metrics.METRICS['gets'].inc()
        resp = utils.retryable('get', TENANTS_URL, headers=headers)
        prometheus_metrics.METRICS['get_successes'].inc()
        tenants_headers = \
            [tenant_header_info(t["external_tenant"]) for t in resp.json()]

        LOGGER.info('Fetching data for ALL(%s) Tenants', len(tenants_headers))

        for tenant_header in tenants_headers:
            LOGGER.debug('%s: ---START Account# %s---',
                         thread.name, tenant_header['acct_no'])
            headers = tenant_header['headers']
            data_size = topological_inventory_data(_, source_id, dest,
                                                   headers, thread)
            prometheus_metrics.METRICS['data_size'].observe(data_size)
            utils.set_processed(tenant_header['acct_no'])
            LOGGER.debug('%s: ---END Account# %s---',
                         thread.name, tenant_header['acct_no'])
    else:
        LOGGER.info('Fetching data for current Tenant')
        data_size = topological_inventory_data(_, source_id, dest,
                                               headers, thread)
        prometheus_metrics.METRICS['data_size'].observe(data_size)
        utils.set_processed(account_id)

    LOGGER.debug('%s: Done, exiting', thread.name)


def topological_inventory_data(
        _: str,
        source_id: str,
        dest: str,
        headers: dict,
        thread
) -> int:
    """Generate Tenant data for topological inventory.

    Parameters
    ----------
    _ (str)
        Skipped
    source_id (str)
        Job identifier
    dest (str)
        URL where to pass data
    headers (dict)
        RH Identity header
    thread
        Current Thread

    """
    # Build the POST data object
    data = {
        'id': source_id,
        'data': {}
    }

    if not APP_CONFIG:
        LOGGER.error('%s: No queries specified', thread.name)
        return 0

    for entity in APP_CONFIG:
        query_spec = QUERIES[entity]

        if query_spec.get('sub_collection'):
            try:
                all_data = _query_sub_collection(
                    query_spec,
                    data['data'],
                    headers=headers
                )
            except utils.RetryFailedError as exception:
                prometheus_metrics.METRICS['get_errors'].inc()
                LOGGER.error(
                    '%s: Unable to fetch source data for "%s": %s',
                    thread.name, source_id, exception
                )
                return 0
        else:
            try:
                all_data = _query_main_collection(query_spec, headers=headers)
            except utils.RetryFailedError as exception:
                prometheus_metrics.METRICS['get_errors'].inc()
                LOGGER.error(
                    '%s: Unable to fetch source data for "%s": %s',
                    thread.name, source_id, exception
                )
                return 0

        LOGGER.info(
            '%s: %s: %s\t%s',
            thread.name, source_id, entity, len(all_data)
        )

        if all_data:
            data['data'][entity] = all_data
        else:
            LOGGER.debug(
                '%s: Inadequate Topological Inventory data for this account.',
                thread.name
            )
            return 0

    # Pass to next service
    prometheus_metrics.METRICS['posts'].inc()
    try:
        utils.retryable('post', dest, json=data, headers=headers)
        prometheus_metrics.METRICS['post_successes'].inc()
    except utils.RetryFailedError as exception:
        LOGGER.error(
            '%s: Failed to pass data for "%s": %s',
            thread.name, source_id, exception
        )
        prometheus_metrics.METRICS['post_errors'].inc()

    return get_deep_size(data['data'])


def tenant_header_info(acct_no):
    """Return a dict containing Account No. and it's b64 Identity header."""
    rh_identity = dict(identity=dict(account_number=acct_no))
    b64_identity = base64.b64encode(json.dumps(rh_identity).encode())
    return {'acct_no': acct_no, 'headers': {'x-rh-identity': b64_identity}}
