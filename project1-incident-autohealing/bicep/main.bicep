@description('AIOps Incident Auto-Healing Infrastructure')
param location string = resourceGroup().location
param environment string = 'dev'
param openAiSkuName string = 'S0'
param alertEmail string

var prefix = 'aiops-${environment}'

// ── Azure OpenAI ─────────────────────────────────────────────────────────────
resource openAi 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: 'oai-${prefix}'
  location: 'eastus'   // OpenAI available in limited regions
  sku: { name: openAiSkuName }
  kind: 'OpenAI'
  properties: {
    customSubDomainName: 'oai-${prefix}'
    publicNetworkAccess: 'Enabled'
  }
}

resource gpt4oDeployment 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: openAi
  name: 'gpt-4o'
  sku: { name: 'Standard', capacity: 10 }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4o'
      version: '2024-05-13'
    }
  }
}

// ── Log Analytics Workspace ───────────────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'law-${prefix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 90
  }
}

// ── App Insights (for the Function itself) ────────────────────────────────────
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ── Key Vault (stores OpenAI key, Slack webhook) ──────────────────────────────
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${prefix}'
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enableRbacAuthorization: true
  }
}

// ── Storage for Function App ──────────────────────────────────────────────────
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'st${replace(prefix, '-', '')}func'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

// ── App Service Plan (Consumption for Function) ───────────────────────────────
resource funcPlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: 'asp-${prefix}-func'
  location: location
  sku: { name: 'Y1', tier: 'Dynamic' }
  kind: 'functionapp'
}

// ── Azure Function App ────────────────────────────────────────────────────────
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
        { name: 'AzureWebJobsStorage',                  value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value}' }
        { name: 'FUNCTIONS_EXTENSION_VERSION',           value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME',              value: 'python' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'LOG_ANALYTICS_WORKSPACE_ID',            value: logAnalytics.properties.customerId }
        { name: 'AZURE_OPENAI_ENDPOINT',                 value: openAi.properties.endpoint }
        { name: 'AZURE_OPENAI_DEPLOYMENT',               value: 'gpt-4o' }
        { name: 'KEY_VAULT_URL',                         value: keyVault.properties.vaultUri }
        // Secrets fetched from Key Vault at runtime via Key Vault references
        { name: 'AZURE_OPENAI_API_KEY',  value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=openai-api-key)' }
        { name: 'SLACK_WEBHOOK_URL',     value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=slack-webhook-url)' }
      ]
    }
  }
}

// Grant Function App access to Key Vault
resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, funcApp.id, 'kvsecretsuser')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: funcApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppUrl string = 'https://${funcApp.properties.defaultHostName}'
output openAiEndpoint string = openAi.properties.endpoint
output logAnalyticsWorkspaceId string = logAnalytics.properties.customerId
