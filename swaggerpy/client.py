#
# Copyright (c) 2013, Digium, Inc.
#

"""Swagger client library.
"""

import logging
import os.path
import re
import urllib
import swagger_type
import swaggerpy
from collections import namedtuple
from response import SwaggerResponse
from swagger_model import create_model_type
from swaggerpy.http_client import SynchronousHttpClient
from swaggerpy.processors import WebsocketProcessor, SwaggerProcessor
from urlparse import urlparse

log = logging.getLogger(__name__)


class ClientProcessor(SwaggerProcessor):
    """Enriches swagger models for client processing.
    """

    def process_resource_listing_api(self, resources, listing_api, context):
        """Add name to listing_api.

        :param resources: Resource listing object
        :param listing_api: ResourceApi object.
        :type context: ParsingContext
        :param context: Current context in the API.
        """
        name, ext = os.path.splitext(os.path.basename(listing_api[u'path']))
        listing_api[u'name'] = name


class Operation(object):
    """Operation object.
    """

    def __init__(self, uri, operation, http_client, models):
        self._uri = uri
        self._json = operation
        self._http_client = http_client
        self._models = models
        self.__doc__ = create_operation_docstring(operation)

    def __repr__(self):
        return u"%s(%s)" % (self.__class__.__name__, self._json[u'nickname'])

    def __call__(self, **kwargs):
        log.info(u"%s?%r" % (self._json[u'nickname'], urllib.urlencode(kwargs)))
        method = self._json[u'method']
        uri = self._uri
        params = {}
        data = None
        headers = None
        for param in self._json.get(u'parameters', []):
            pname = param[u'name']
            # ToDo: No check on param value right now.
            # To be done similar to checkResponse in SwaggerResponse
            value = kwargs.get(pname)
            paramType = param[u'paramType']
            # Turn list params into comma separated values
            # Assumption: values will only be primitive else str() fails
            if isinstance(value, list) and paramType in ('path', 'query'):
                value = u",".join(str(x) for x in value)

            if value:
                if paramType == u'path':
                    uri = uri.replace(u'{%s}' % pname, unicode(value))
                elif paramType == u'query':
                    params[pname] = value
                elif paramType == u'body':
                    # value if not string is converted to json.dumps() later
                    # ToDo: model instance as body object not valid right now
                    #       Must be given as a json string in the body
                    data = value
                    headers = {'content-type': 'application/json'}
                else:
                    raise AssertionError(
                        u"Unsupported paramType %s" % param.paramType)
                del kwargs[pname]
            else:
                if param.get(u'required'):
                    raise TypeError(
                        u"Missing required parameter '%s' for '%s'" %
                        (pname, self._json[u'nickname']))
        if kwargs:
            raise TypeError(u"'%s' does not have parameters %r" %
                            (self._json[u'nickname'], kwargs.keys()))

        log.info(u"%s %s(%r)", method, uri, params)
        if self._json[u'is_websocket']:
            # Fix up http: URLs
            uri = re.sub(u'^http', u"ws", uri)
            response = self._http_client.ws_connect(uri, params=params)
        else:
            response = self._http_client.request(method, uri, params, data, headers)
        _type = swagger_type.get_swagger_type(self._json)
        value = None
        if self._http_client.is_response_ok(response) and response.text:
            # Validate and then convert API response to Python model instance
            value = SwaggerResponse(response.json(), _type, self._models).parse_object()
        setattr(response, 'value', value)
        return response


class Resource(object):
    """Swagger resource, described in an API declaration.

    :param resource: Resource model
    :param http_client: HTTP client API
    """

    def __init__(self, resource, http_client, basePath):
        log.debug(u"Building resource '%s'" % resource[u'name'])
        self._json = resource
        decl = resource['api_declaration']
        self._http_client = http_client
        self._basePath = basePath
        self._set_models()
        self._operations = dict(
                (oper['nickname'], self._build_operation(decl, api, oper))
            for api in decl['apis']
            for oper in api['operations'])
        for key in self._operations:
            setattr(self, key, self._get_operation(key))

    def _set_models(self):
        """Create namedtuple of model types created from 'api_declaration'
        """
        models_dict = self._json['api_declaration'].get('models', {})
        models = namedtuple('models', models_dict.keys())
        keys = {}
        for key in models_dict.keys():
            keys[key] = create_model_type(models_dict[key])
        self.models = models(**keys)

    def __repr__(self):
        return u"%s(%s)" % (self.__class__.__name__, self._json[u'name'])

    def __getattr__(self, item):
        """Promote operations to be object fields.

        :param item: Name of the attribute to get.
        :rtype: Resource
        :return: Resource object.
        """
        op = self._get_operation(item)
        if not op:
            raise AttributeError(u"Resource '%s' has no operation '%s'" %
                                 (self._get_name(), item))
        return op

    def _get_operation(self, name):
        """Gets the operation with the given nickname.

        :param name: Nickname of the operation.
        :rtype:  Operation
        :return: Operation, or None if not found.
        """
        return self._operations.get(name)

    def _get_name(self):
        """Returns the name of this resource.

        Name is derived from the filename of the API declaration.

        :return: Resource name.
        """
        return self._json.get(u'name')

    def _build_operation(self, decl, api, operation):
        """Build an operation object

        :param decl: API declaration.
        :param api: API entry.
        :param operation: Operation.
        """
        log.debug(u"Building operation %s.%s" % (
            self._get_name(), operation[u'nickname']))
        #If basePath is root, use the basePath stored during init
        basePath = self._basePath if decl[u'basePath'] == '/' else decl[u'basePath']
        uri = basePath + api[u'path']
        return Operation(uri, operation, self._http_client, self.models)


class SwaggerClient(object):
    """Client object for accessing a Swagger-documented RESTful service.

    :param url_or_resource: Either the parsed resource listing+API decls, or
                            its URL.
    :type url_or_resource: dict or str
    :param http_client: HTTP client API
    :type  http_client: HttpClient
    """

    def __init__(self, url_or_resource, http_client=None):
        if not http_client:
            http_client = SynchronousHttpClient()
        self._http_client = http_client

        loader = swaggerpy.Loader(
            http_client, [WebsocketProcessor(), ClientProcessor()])

        # url_or_resource can be url of type str,
        # OR a dict of resource itself.
        if isinstance(url_or_resource, (str, unicode)):
            log.debug(u"Loading from %s" % url_or_resource)
            self._api_docs = loader.load_resource_listing(url_or_resource)
            parsed_uri = urlparse(url_or_resource)
            basePath = "{uri.scheme}://{uri.netloc}".format(uri=parsed_uri)
        else:
            log.debug(u"Loading from %s" % url_or_resource.get(u'basePath'))
            self._api_docs = url_or_resource
            loader.process_resource_listing(self._api_docs)
            basePath = url_or_resource.get(u'basePath')

        self._resources = {}
        for resource in self._api_docs[u'apis']:
            self._resources[resource[u'name']] = Resource(resource, http_client, basePath)
            setattr(self, resource['name'], self._get_resource(resource[u'name']))

    def __repr__(self):
        return u"%s(%s)" % (self.__class__.__name__, self._api_docs.get(u'basePath'))

    def __getattr__(self, item):
        """Promote resource objects to be client fields.

        :param item: Name of the attribute to get.
        :return: Resource object.
        """
        resource = self._get_resource(item)
        if not resource:
            raise AttributeError(u"API has no resource '%s'" % item)
        return resource

    def close(self):
        """Close the SwaggerClient, and underlying resources.
        """
        self._http_client.close()

    def _get_resource(self, name):
        """Gets a Swagger resource by name.

        :param name: Name of the resource to get
        :rtype: Resource
        :return: Resource, or None if not found.
        """
        return self._resources.get(name)


def __build_param_string(param):
    """Builds param docstring from the param dict

       :param param: data to create docstring from
       :type param: dict
       :returns: string giving meta info
    """
    string = "\t" + param.get("name")
    _type = param.get('$ref') or param.get('format') or param.get('type')
    if _type:
        string += (" (%s) " % _type)
    if param.get('description'):
        string += ": " + param["description"]
    return string + "\n"


def create_operation_docstring(_json):
    """Builds Operation docstring from the json dict

       :param _json: data to create docstring from
       :type _json: dict
       :returns: string giving meta info
    """
    docstring = ""
    if _json.get('summary'):
        docstring += ("[%s] %s\n\n" % (_json['method'], _json.get('summary')))
    docstring += (_json["notes"] + "\n") if _json.get("notes") else ''

    if _json["parameters"]:
        docstring += "Args:\n"
        for param in _json["parameters"]:
            docstring += __build_param_string(param)
    if _json.get('type'):
        docstring += "Returns:\n\t%s\n" % _json["type"]
    if _json.get('responseMessages'):
        docstring += "Raises:\n"
        for msg in _json.get('responseMessages'):
            docstring += "\t%s: %s\n" % (msg.get("code"), msg.get("message"))
    return docstring