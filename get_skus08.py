import re
import time
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from azure.mgmt.resource import ResourceManagementClient
from tabulate import tabulate
from tqdm import tqdm
from azure.mgmt.resource import SubscriptionClient

def get_reserved_instances(subscription_id):
    credential = DefaultAzureCredential()
    resource_graph_client = ResourceGraphClient(credential)

    query = "Resources | where type =~ 'Microsoft.Compute/reservations'"
    request = QueryRequest(subscriptions=[subscription_id], query=query)
    response = resource_graph_client.resources(request)

    reserved_instances = set()
    for resource in response.data:
        reserved_instances.add(resource['name'])
    
    return reserved_instances

def validate_aks_sku_with_arm(resource_group, cluster_name, sku_name, subscription_id, credential):
    """Use ARM template validation to test AKS SKU compatibility without creating resources"""
    try:
        resource_client = ResourceManagementClient(credential, subscription_id)
        
        # ARM template for AKS node pool validation
        template = {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {
                "clusterName": {"type": "string"},
                "nodePoolName": {"type": "string"}, 
                "vmSize": {"type": "string"}
            },
            "resources": [{
                "type": "Microsoft.ContainerService/managedClusters/agentPools",
                "apiVersion": "2023-05-01",
                "name": "[concat(parameters('clusterName'), '/', parameters('nodePoolName'))]",
                "properties": {
                    "count": 1,
                    "vmSize": "[parameters('vmSize')]",
                    "osType": "Linux",
                    "mode": "User"
                }
            }]
        }
        
        # Parameters for validation
        parameters = {
            "clusterName": {"value": cluster_name},
            "nodePoolName": {"value": f"validate-{sku_name.lower().replace('_', '-')}"[:12]},  # Keep it short
            "vmSize": {"value": sku_name}
        }
        
        # Deployment properties for validation
        deployment_properties = {
            'mode': 'Incremental',
            'template': template,
            'parameters': parameters
        }
        
        # Validate deployment (doesn't create resources)
        validation_result = resource_client.deployments.begin_validate(
            resource_group_name=resource_group,
            deployment_name=f"validate-sku-{int(time.time())}",  # Unique name
            parameters={'properties': deployment_properties}
        ).result()  # Wait for validation to complete
        
        # Check if validation passed
        if hasattr(validation_result, 'error') and validation_result.error:
            error_msg = "Unknown ARM validation error"
            if hasattr(validation_result.error, 'message'):
                error_msg = validation_result.error.message
            elif hasattr(validation_result.error, 'details') and validation_result.error.details:
                error_msg = validation_result.error.details[0].message if validation_result.error.details[0].message else "ARM validation failed"
            
            return False, f"ARM validation failed: {error_msg}"
        else:
            return True, "ARM validation passed - SKU compatible with AKS"
            
    except Exception as e:
        return False, f"ARM validation error: {str(e)}"

def check_basic_aks_compatibility(sku_name):
    """Basic AKS compatibility check for known incompatible SKUs"""
    # Known AKS incompatible SKUs
    incompatible_skus = [
        'Basic_A0', 'Basic_A1', 'Standard_A0', 'Standard_A1', 'Standard_A1_v2'
    ]
    
    if sku_name in incompatible_skus:
        return 'No', 'Known AKS incompatible SKU'
    
    # Basic size checks
    if any(x in sku_name.lower() for x in ['a0', 'a1']):
        return 'Unlikely', 'May not meet AKS minimum requirements'
    
    return 'Likely', 'Appears AKS compatible'

def list_vm_skus(region, pattern, test_aks=False, resource_group=None, cluster_name=None, use_arm_validation=False):
    start_time = time.time()
    
    credential = DefaultAzureCredential()
    subscription_client = SubscriptionClient(credential)
    subscription_id = next(subscription_client.subscriptions.list()).subscription_id
    compute_client = ComputeManagementClient(credential, subscription_id)

    reserved_instances = get_reserved_instances(subscription_id)
    
    skus = compute_client.resource_skus.list()
    table = []
    
    # Filter to VM SKUs only when testing AKS
    filtered_skus = [sku for sku in skus if sku.resource_type == "virtualMachines"] if test_aks else list(skus)
    
    for sku in tqdm(filtered_skus, desc="Loading SKUs" + (" and testing AKS compatibility" if test_aks else "")):
        if sku.locations and region in sku.locations:
            if pattern == "all" or re.search(pattern, sku.name, re.IGNORECASE):
                zones = sku.location_info[0].zones if sku.location_info else []
                sku_pressure = sku.resource_type
                sku_quota = next((capability.value for capability in sku.capabilities if capability.name == 'MaxResourceCount'), 'Unknown') if sku.capabilities else 'Unknown'
                allowed = 'Yes' if not sku.restrictions else 'No'
                reserved = 'Yes' if sku.name in reserved_instances else 'No'
                
                # AKS compatibility testing
                if test_aks:
                    if use_arm_validation and resource_group and cluster_name:
                        # Use ARM template validation
                        is_compatible, notes = validate_aks_sku_with_arm(
                            resource_group, cluster_name, sku.name, subscription_id, credential
                        )
                        aks_compatible = 'Yes' if is_compatible else 'No'
                        aks_notes = notes
                    else:
                        # Use basic compatibility check
                        aks_compatible, aks_notes = check_basic_aks_compatibility(sku.name)
                    
                    table.append([sku.name, ', '.join(zones) if zones else 'None', sku_pressure, sku_quota, allowed, reserved, aks_compatible, aks_notes])
                else:
                    table.append([sku.name, ', '.join(zones) if zones else 'None', sku_pressure, sku_quota, allowed, reserved])
    
    # Headers
    if test_aks:
        headers = ["SKU", "Zones", "Resource Type", "Quota", "Allowed", "Reserved", "AKS Compatible", "AKS Notes"]
    else:
        headers = ["SKU", "Zones", "Resource Type", "Quota", "Allowed", "Reserved"]
    
    print(tabulate(table, headers, tablefmt="grid"))
    
    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Time taken: {elapsed_time:.2f} seconds")

if __name__ == "__main__":
    region = input("Enter the region: ")
    pattern = input("Enter SKU pattern (or 'all' to list all SKUs): ")
    
    test_aks = input("Test AKS compatibility? (y/n): ").lower().startswith('y')
    
    resource_group = None
    cluster_name = None
    use_arm_validation = False
    
    if test_aks:
        use_arm = input("Use ARM template validation (requires existing AKS cluster)? (y/n): ").lower().startswith('y')
        if use_arm:
            resource_group = input("Enter AKS resource group name: ")
            cluster_name = input("Enter AKS cluster name: ")
            use_arm_validation = True
            print("Note: ARM validation tests SKU compatibility without creating resources.")
    
    list_vm_skus(region, pattern, test_aks, resource_group, cluster_name, use_arm_validation)