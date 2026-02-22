#!/bin/bash

echo "==============================="
echo "AWS RESOURCE AUDIT (ALL REGIONS)"
echo "==============================="

REGIONS=$(aws ec2 describe-regions --query "Regions[].RegionName" --output text)

for region in $REGIONS; do
  echo ""
  echo "==============================="
  echo "Region: $region"
  echo "==============================="

  echo "🔎 EC2 Instances (Running)"
  aws ec2 describe-instances --region $region \
    --filters Name=instance-state-name,Values=running \
    --query "Reservations[].Instances[].InstanceId" \
    --output table

  echo "🔎 ECS Clusters"
  aws ecs list-clusters --region $region --output table

  echo "🔎 EKS Clusters"
  aws eks list-clusters --region $region --output table

  echo "🔎 NAT Gateways"
  aws ec2 describe-nat-gateways --region $region \
    --query "NatGateways[].NatGatewayId" \
    --output table

  echo "🔎 Elastic IPs"
  aws ec2 describe-addresses --region $region \
    --query "Addresses[].PublicIp" \
    --output table

  echo "🔎 RDS Instances"
  aws rds describe-db-instances --region $region \
    --query "DBInstances[].DBInstanceIdentifier" \
    --output table

done

echo ""
echo "==============================="
echo "AUDIT COMPLETE"
echo "==============================="
