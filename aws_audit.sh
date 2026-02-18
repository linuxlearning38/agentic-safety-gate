#!/bin/bash

echo "==============================="
echo "AWS RESOURCE AUDIT START"
echo "==============================="

echo ""
echo "🔎 EC2 Instances (Running)"
aws ec2 describe-instances \
  --filters Name=instance-state-name,Values=running \
  --query "Reservations[].Instances[].{ID:InstanceId,Type:InstanceType,State:State.Name,PublicIP:PublicIpAddress}" \
  --output table

echo ""
echo "🔎 Elastic IPs"
aws ec2 describe-addresses \
  --query "Addresses[].{PublicIP:PublicIp,InstanceId:InstanceId}" \
  --output table

echo ""
echo "🔎 NAT Gateways"
aws ec2 describe-nat-gateways \
  --query "NatGateways[].{ID:NatGatewayId,State:State}" \
  --output table

echo ""
echo "🔎 EBS Volumes (Available & In-use)"
aws ec2 describe-volumes \
  --query "Volumes[].{ID:VolumeId,State:State,Size:Size}" \
  --output table

echo ""
echo "🔎 Load Balancers"
aws elbv2 describe-load-balancers \
  --query "LoadBalancers[].{Name:LoadBalancerName,State:State.Code}" \
  --output table

echo ""
echo "🔎 RDS Instances"
aws rds describe-db-instances \
  --query "DBInstances[].{ID:DBInstanceIdentifier,Status:DBInstanceStatus}" \
  --output table

echo ""
echo "🔎 S3 Buckets"
aws s3 ls

echo ""
echo "🔎 Lambda Functions"
aws lambda list-functions \
  --query "Functions[].FunctionName" \
  --output table

echo ""
echo "🔎 CloudWatch Log Groups"
aws logs describe-log-groups \
  --query "logGroups[].logGroupName" \
  --output table

echo ""
echo "==============================="
echo "AUDIT COMPLETE"
echo "==============================="
