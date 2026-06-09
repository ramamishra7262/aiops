@description('AIOps Anomaly Detection Pipeline Infrastructure')
param location string = resourceGroup().location
param environment string = 'dev'

var prefix = 'aiops-anomaly-${environment}'

resource anomalyDetector 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: 'anom-${prefix}'
  location: location
  sku: { name: 'S0' }
  kind: 'AnomalyDetector'
  properties: {
    customSubDomainName: 'anom-${prefix}'
    publicNetworkAccess: 'Enabled'
  }
}

resource eventHubNamespace 'Microsoft.EventHub/namespaces@2022-10-01-preview' = {
  name: 'evhns-${prefix}'
  location: location
  sku: { name: 'Standard', tier: 'Standard', capacity: 2 }
  properties: {
    isAutoInflateEnabled: true
    maximumThroughputUnits: 10
  }
}

resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2022-10-01-preview' = {
  parent: eventHubNamespace
  name: 'aiops-metrics'
  properties: {
    messageRetentionInDays: 3
    partitionCount: 4
  }
}

resource consumerGroup 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2022-10-01-preview' = {
  parent: eventHub
  name: 'anomaly-detector'
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'law-${prefix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'st${replace(prefix, '-', '')}fn'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false }
}

resource funcPlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: 'asp-${prefix}'
  location: location
  sku: { name: 'EP1', tier: 'ElasticPremium' }  // Premium for consistent Event Hub processing
  kind: 'elastic'
}

resource funcApp 'Microsoft.Web/sites@2023-01-01' = {
  name: 'func-${prefix}'
  location: location
  kind: 'functionapp'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: funcPlan.id
    httpsOnly: true
    siteConfig: {
      pythonVersion: '3.11'
      appSettings: [
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value}' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'ANOMALY_DETECTOR_ENDPOINT', value: anomalyDetector.properties.endpoint }
        { name: 'EventHubConnection', value: 'Endpoint=sb://${eventHubNamespace.name}.servicebus.windows.net/;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=${listKeys('${eventHubNamespace.id}/AuthorizationRules/RootManageSharedAccessKey', '2022-10-01-preview').primaryKey}' }
      ]
    }
  }
}

output anomalyDetectorEndpoint string = anomalyDetector.properties.endpoint
output eventHubConnectionString string = 'Endpoint=sb://${eventHubNamespace.name}.servicebus.windows.net/'
output functionAppName string = funcApp.name
