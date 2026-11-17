"""The hand-picked part of Grafana's extra graph (module.py's extra_graph; the generated part is settings_types.go): keys
of a provisioned datasource's jsonData and secureJsonData that the code reads by key from a map, so no Go type in the
graph has them. Each entry names its reader (Grafana v13.1.0, and the go.mod versions of its dependencies: plugin SDK
v0.292.1, promlib v0.0.11, grafana-aws-sdk v1.4.4), and the kind it reads the value as. extra_graph turns each key into an
extra edge (datasources.type=<plugin>.jsonData.<key>, or .secureJsonData.<key>) to a values.* type of that kind.

Left out:
- Plugins with no source here: elasticsearch and zipkin (external).
- azure monitor's and mssql's Azure credentials (azcredentials.FromDatasourceData reads the whole map; not traced).
- The Grafana server's own reads of the HTTP options (SERVER_JSON) for plugins with a backend: their backends read the
  same options through the SDK (HTTP_JSON).
- httpHeaderName<i>/httpHeaderValue<i> for i > 1: the SDK reads i = 1, 2, ... until a name is missing; i = 1 stands
  for them.
"""

# Kind => the values.* type an edge for a key of that kind goes to (the provisioning file's own scalar and map types)
VALUES = "github.com/grafana/grafana/pkg/services/provisioning/values."
KIND_TYPES = {"string": VALUES + "StringValue", "bool": VALUES + "BoolValue", "number": VALUES + "IntValue",
              "map": VALUES + "JSONValue"}

# Plugins whose backend builds its HTTP client with the plugin SDK's DataSourceInstanceSettings.HTTPClientOptions, so
# reads the shared HTTP options below: `grep -rln 'HTTPClientOptions(' pkg/tsdb` (plugin ids: coreplugin.go), plus
# prometheus, through promlib (client/transport.go:20)
HTTP_PLUGINS = ["cloudwatch", "grafana-azure-monitor-datasource", "grafana-pyroscope-datasource", "graphite",
                "influxdb", "jaeger", "loki", "mysql", "opentsdb", "parca", "prometheus", "stackdriver", "tempo"]

# The shared HTTP options: plugin SDK backend/http_settings.go parseHTTPSettings (:118-291, jsonData as a map) and
# backend/common.go HTTPClientOptions (:127-134, 342)
HTTP_JSON = {
    "access": "string", "url": "string", "basicAuth": "bool", "basicAuthUser": "string",
    "timeout": "number", "dialTimeout": "number", "httpKeepAlive": "number", "httpTLSHandshakeTimeout": "number",
    "httpExpectContinueTimeout": "number", "httpMaxConnsPerHost": "number", "httpMaxIdleConns": "number",
    "httpMaxIdleConnsPerHost": "number", "httpIdleConnTimeout": "number",
    "tlsAuth": "bool", "tlsAuthWithCACert": "bool", "tlsSkipVerify": "bool", "serverName": "string",
    "sigV4Auth": "bool", "sigV4Region": "string", "sigV4AssumeRoleArn": "string", "sigV4AuthType": "string",
    "sigV4ExternalId": "string", "sigV4Profile": "string",
    "httpHeaderName1": "string",
}
HTTP_SECURE = {
    "basicAuthPassword": "string", "password": "string",  # (common.go:130, 134: password if basicAuthPassword is unset)
    "tlsCACert": "string", "tlsClientCert": "string", "tlsClientKey": "string",
    "sigV4AccessKey": "string", "sigV4SecretKey": "string", "sigV4SessionToken": "string",
    "httpHeaderValue1": "string", "secureSocksProxyPassword": "string",
}

# Plugins with no backend that Grafana's own code reaches over HTTP, so through its datasource service's HTTP options
# (SERVER_JSON): alertmanager, through the data proxy (pkg/services/ngalert/api/lotex_am.go:77) and the alert sender
# (pkg/services/ngalert/sender/router.go: datasourceToExternalAMcfg reads tlsAuth, tlsSkipVerify, tlsClientCert/Key,
# :301-312, and the custom headers, :291)
SERVER_PLUGINS = ["alertmanager"]

# The Grafana server's HTTP options: pkg/services/datasources/service/datasource.go (simplejson reads and
# DecryptedValue): basic auth :801-810, secure socks proxy :887-903, timeouts :906-909 and :997, sigV4 :916-934, TLS
# :951-981, custom headers getCustomHeaders :1015 (httpHeaderName<i>, as the SDK's)
SERVER_JSON = {
    "timeout": "number", "keepAlive": "number", "secureSocksProxyUsername": "string",
    "tlsAuth": "bool", "tlsAuthWithCACert": "bool", "tlsSkipVerify": "bool", "serverName": "string",
    "sigV4Auth": "bool", "sigV4Region": "string", "sigV4AssumeRoleArn": "string", "sigV4AuthType": "string",
    "sigV4ExternalId": "string", "sigV4Profile": "string", "httpHeaderName1": "string",
}
SERVER_SECURE = {
    "basicAuthPassword": "string", "password": "string", "secureSocksProxyPassword": "string",
    "tlsCACert": "string", "tlsClientCert": "string", "tlsClientKey": "string",
    "sigV4AccessKey": "string", "sigV4SecretKey": "string", "httpHeaderValue1": "string",
}

# Read by Grafana's data proxy for every datasource (pkg/apis/datasource/v0alpha1/unstructured.go:138, used from
# pkg/api/pluginproxy/ds_proxy.go:267): a list of cookie names, so a string per element
PROXY_JSON = {"keepCookies": "string"}

# Plugin-specific keys read by key: plugin => {key: (kind, reader)}
PLUGIN_JSON = {
    "prometheus": {
        "httpMethod": ("string", "promlib client/transport.go:29, querydata/request.go:62, resource/resource.go:38"),
        "timeInterval": ("string", "promlib querydata/request.go:67"),
        "customQueryParameters": ("string", "promlib middleware/custom_query_params.go:14"),
        "prometheusType": ("string", "pkg/services/ngalert/writer/datasourcewriter.go:107"),
    },
    "tempo": {"streamingEnabled": ("map", "pkg/tsdb/tempo/tempo.go:178")},
    # No backend: Grafana's alerting reads them (simplejson)
    "alertmanager": {
        "implementation": ("string", "pkg/services/ngalert/sender/router.go:339, api/lotex_am.go:88"),
        "handleGrafanaManagedAlerts": ("bool", "pkg/services/ngalert/sender/router.go:264"),
        "tlsSkipVerify": ("bool", "pkg/services/ngalert/sender/router.go:301"),
    },
}
PLUGIN_SECURE = {
    "cloudwatch": {k: ("string", "grafana-aws-sdk pkg/awsds/settings.go:135-138")
                   for k in ("accessKey", "secretKey", "sessionToken", "proxyPassword")},
    "influxdb": {"token": ("string", "pkg/tsdb/influxdb/influxdb.go")},
    "grafana-postgresql-datasource": {
        "password": ("string", "pkg/tsdb/grafana-postgresql-datasource/postgres.go"),
        **{k: ("string", "pkg/tsdb/grafana-postgresql-datasource/tlsmanager.go")
           for k in ("tlsCACert", "tlsClientCert", "tlsClientKey")}},
    "mysql": {"password": ("string", "pkg/tsdb/mysql/mysql.go")},
    "mssql": {"password": ("string", "pkg/tsdb/mssql/sqleng/connection.go")},
}

# Other edges the code decodes into, but not through a type the graph can follow
# (parent, Go field, child, key): a notification policy's list item is decoded whole into definition.Route
# (NotificiationPolicyV1.UnmarshalYAML captures it in its yaml:"-" Policy field; mapToModel,
# pkg/services/provisioning/alerting/notification_policy_types.go:33-43, decodes it into definitions.Route, an alias
# of github.com/grafana/alerting/definition.Route): Route's keys are the item's own
OTHER_EDGES = [
    ("github.com/grafana/grafana/pkg/services/provisioning/alerting.NotificiationPolicyV1", "Policy",
     "github.com/grafana/alerting/definition.Route", ""),
]


def keys():
    """(plugin, field, key, kind) for every key read by key: field is the Go field (JSONData, SecureJSONData)"""
    out = []
    for p in HTTP_PLUGINS:
        out += [(p, "JSONData", k, kind) for k, kind in {**HTTP_JSON, **PROXY_JSON}.items()]
        out += [(p, "SecureJSONData", k, kind) for k, kind in HTTP_SECURE.items()]
    for p in SERVER_PLUGINS:
        out += [(p, "JSONData", k, kind) for k, kind in {**SERVER_JSON, **PROXY_JSON}.items()]
        out += [(p, "SecureJSONData", k, kind) for k, kind in SERVER_SECURE.items()]
    for field, table in (("JSONData", PLUGIN_JSON), ("SecureJSONData", PLUGIN_SECURE)):
        for p, ks in table.items():
            out += [(p, field, k, kind) for k, (kind, _) in ks.items()]
    return sorted(set(out))
