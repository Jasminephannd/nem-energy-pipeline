// =============================================================================
// NEM Energy Pipeline - core Azure infrastructure
//
// Deploys the Day 1 foundation:
//   - ADLS Gen2 storage (hierarchical namespace) with bronze/silver/rejected
//   - Key Vault (RBAC authorisation) for pipeline secrets
//   - Azure SQL logical server + serverless database for the gold star schema
//   - Data Factory with a system-assigned managed identity
//   - Role assignments so ADF can reach storage and Key Vault without keys
//
// Deploy:
//   az deployment group create -g <rg> -f infra/main.bicep -p ...
// =============================================================================

targetScope = 'resourceGroup'

// -----------------------------------------------------------------------------
// Parameters
// -----------------------------------------------------------------------------

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Short prefix used to build resource names.')
@minLength(2)
@maxLength(5)
param projectPrefix string = 'nem'

@description('Administrator login for the Azure SQL logical server.')
param sqlAdminLogin string = 'nemadmin'

@description('Administrator password for the SQL server. Pass at deploy time - never commit this.')
@secure()
param sqlAdminPassword string

@description('Your current public IP address, allowed through the SQL firewall.')
param clientIpAddress string

@description('Object ID of the deploying user, granted data-plane access to storage and Key Vault.')
param deployerObjectId string

// -----------------------------------------------------------------------------
// Variables
// -----------------------------------------------------------------------------

// uniqueString() is deterministic per resource group, so redeploys reuse names.
var uniqueSuffix = uniqueString(resourceGroup().id)

var storageAccountName = toLower('st${projectPrefix}${uniqueSuffix}')
var keyVaultName = 'kv-${projectPrefix}-${uniqueSuffix}'
var sqlServerName = 'sql-${projectPrefix}-${uniqueSuffix}'
var dataFactoryName = 'adf-${projectPrefix}-${uniqueSuffix}'
var sqlDatabaseName = 'sqldb-nem-gold'

var containerNames = [
  'bronze'
  'silver'
  'rejected'
]

// Built-in role definition IDs (stable across all tenants).
var roles = {
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
}

var tags = {
  project: 'nem-energy-pipeline'
  purpose: 'portfolio'
  costCentre: 'personal'
}

// -----------------------------------------------------------------------------
// Storage - ADLS Gen2
// -----------------------------------------------------------------------------

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    // Hierarchical namespace is what makes this ADLS Gen2 rather than plain blob.
    // It cannot be enabled after creation - the account would have to be rebuilt.
    isHnsEnabled: true
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = [
  for name in containerNames: {
    parent: blobService
    name: name
    properties: {
      publicAccess: 'None'
    }
  }
]

// -----------------------------------------------------------------------------
// Key Vault
// -----------------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    // RBAC rather than legacy access policies - consistent with the managed
    // identity model used everywhere else in this build.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

// -----------------------------------------------------------------------------
// Azure SQL - gold layer
// -----------------------------------------------------------------------------

resource sqlServer 'Microsoft.Sql/servers@2021-11-01' = {
  name: sqlServerName
  location: location
  tags: tags
  properties: {
    administratorLogin: sqlAdminLogin
    administratorLoginPassword: sqlAdminPassword
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2021-11-01' = {
  parent: sqlServer
  name: sqlDatabaseName
  location: location
  tags: tags
  sku: {
    // Serverless General Purpose - bills per second of compute and pauses when idle.
    name: 'GP_S_Gen5_1'
    tier: 'GeneralPurpose'
    family: 'Gen5'
    capacity: 1
  }
  properties: {
    // Pause after 60 minutes idle. This is the main cost control on the database.
    autoPauseDelay: 60
    minCapacity: json('0.5')
    maxSizeBytes: 34359738368 // 32 GB
    zoneRedundant: false
  }
}

// Allows Azure services (including Data Factory) to reach the server.
resource sqlFirewallAzure 'Microsoft.Sql/servers/firewallRules@2021-11-01' = {
  parent: sqlServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// Allows your workstation to connect from SSMS / Azure Data Studio / Power BI.
resource sqlFirewallClient 'Microsoft.Sql/servers/firewallRules@2021-11-01' = {
  parent: sqlServer
  name: 'AllowClientIp'
  properties: {
    startIpAddress: clientIpAddress
    endIpAddress: clientIpAddress
  }
}

// -----------------------------------------------------------------------------
// Data Factory
// -----------------------------------------------------------------------------

resource dataFactory 'Microsoft.DataFactory/factories@2018-06-01' = {
  name: dataFactoryName
  location: location
  tags: tags
  identity: {
    // System-assigned identity removes the need for connection strings or keys
    // when ADF talks to storage and Key Vault.
    type: 'SystemAssigned'
  }
  properties: {}
}

// -----------------------------------------------------------------------------
// Role assignments
//
// guid() builds a deterministic assignment name, so redeploying is idempotent
// rather than failing on a duplicate.
// -----------------------------------------------------------------------------

resource adfStorageAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, dataFactory.id, roles.storageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: dataFactory.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource adfKeyVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, dataFactory.id, roles.keyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: dataFactory.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Without this, the portal shows "you do not have permission" when browsing blobs -
// subscription Owner grants control-plane rights, not data-plane rights.
resource deployerStorageAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, deployerObjectId, roles.storageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: deployerObjectId
    principalType: 'User'
  }
}

resource deployerKeyVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, deployerObjectId, roles.keyVaultSecretsOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
    principalId: deployerObjectId
    principalType: 'User'
  }
}

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

output storageAccountName string = storageAccount.name
output storageDfsEndpoint string = storageAccount.properties.primaryEndpoints.dfs
output keyVaultName string = keyVault.name
output sqlServerFqdn string = sqlServer.properties.fullyQualifiedDomainName
output sqlDatabaseName string = sqlDatabase.name
output dataFactoryName string = dataFactory.name
output dataFactoryPrincipalId string = dataFactory.identity.principalId
