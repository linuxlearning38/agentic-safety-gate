#!/bin/bash

HOURS=730
MAX_EXPOSURE=20
VERBOSE=true
REPORT_FILE="financial_report.json"

# Cost assumptions (conservative)
COST_EC2_SMALL=0.02
COST_NAT=0.045
COST_EKS=0.10
COST_RDS_SMALL=0.025
COST_EIP=0.005
COST_EBS_GB=0.08

echo "=================================================="
echo "AWS TOTAL FINANCIAL GUARD"
echo "Hard ceiling: \$20/month"
echo "=================================================="

START_DATE=$(date +%Y-%m-01)
END_DATE=$(date -d "+1 month" +%Y-%m-01)
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# --------------------------------------------------
# 1️⃣ Month-to-date total
# --------------------------------------------------

ACTUAL=$(aws ce get-cost-and-usage \
  --time-period Start=$START_DATE,End=$END_DATE \
  --granularity MONTHLY \
  --metrics "UnblendedCost" \
  --query "ResultsByTime[0].Total.UnblendedCost.Amount" \
  --output text)

if [ -z "$ACTUAL" ]; then ACTUAL=0; fi

printf "Month-to-date actual spend: \$%.4f\n" "$ACTUAL"

# --------------------------------------------------
# 2️⃣ Month-to-date service breakdown (clean format)
# --------------------------------------------------

echo ""
echo "Month-to-date service breakdown:"
echo ""

aws ce get-cost-and-usage \
  --time-period Start=$START_DATE,End=$END_DATE \
  --granularity MONTHLY \
  --metrics "UnblendedCost" \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query "ResultsByTime[0].Groups[?Metrics.UnblendedCost.Amount!='0'].[Keys[0],Metrics.UnblendedCost.Amount]" \
  --output text | while IFS=$'\t' read -r service amount; do
    printf "  %-60s : \$%.4f\n" "$service" "$amount"
done

# --------------------------------------------------
# 3️⃣ Infra projected scan
# --------------------------------------------------

PROJECTED=0
REGION_JSON=""

REGIONS=$(aws ec2 describe-regions --query "Regions[].RegionName" --output text)

for region in $REGIONS; do

  REGION_TOTAL=0

  if [ "$VERBOSE" = true ]; then
    echo ""
    echo "Scanning region: $region"
  fi

  # EC2
  EC2_COUNT=$(aws ec2 describe-instances \
    --region $region \
    --filters Name=instance-state-name,Values=running \
    --query "Reservations[].Instances[].InstanceId" \
    --output text | wc -w)

  EC2_COST=$(echo "$EC2_COUNT * $COST_EC2_SMALL * $HOURS" | bc -l)
  REGION_TOTAL=$(echo "$REGION_TOTAL + $EC2_COST" | bc -l)
  [ "$VERBOSE" = true ] && printf "  EC2 Compute: \$%.4f\n" "$EC2_COST"

  # NAT
  NAT_COUNT=$(aws ec2 describe-nat-gateways \
    --region $region \
    --query "NatGateways[].NatGatewayId" \
    --output text | wc -w)

  NAT_COST=$(echo "$NAT_COUNT * $COST_NAT * $HOURS" | bc -l)
  REGION_TOTAL=$(echo "$REGION_TOTAL + $NAT_COST" | bc -l)
  [ "$VERBOSE" = true ] && printf "  NAT Gateway: \$%.4f\n" "$NAT_COST"

  # EKS
  EKS_COUNT=$(aws eks list-clusters \
    --region $region \
    --query "clusters[]" \
    --output text | wc -w)

  EKS_COST=$(echo "$EKS_COUNT * $COST_EKS * $HOURS" | bc -l)
  REGION_TOTAL=$(echo "$REGION_TOTAL + $EKS_COST" | bc -l)
  [ "$VERBOSE" = true ] && printf "  EKS: \$%.4f\n" "$EKS_COST"

  # RDS
  RDS_COUNT=$(aws rds describe-db-instances \
    --region $region \
    --query "DBInstances[].DBInstanceIdentifier" \
    --output text | wc -w)

  RDS_COST=$(echo "$RDS_COUNT * $COST_RDS_SMALL * $HOURS" | bc -l)
  REGION_TOTAL=$(echo "$REGION_TOTAL + $RDS_COST" | bc -l)
  [ "$VERBOSE" = true ] && printf "  RDS: \$%.4f\n" "$RDS_COST"

  # Elastic IP
  EIP_COUNT=$(aws ec2 describe-addresses \
    --region $region \
    --query "Addresses[?InstanceId==null].PublicIp" \
    --output text | wc -w)

  EIP_COST=$(echo "$EIP_COUNT * $COST_EIP * $HOURS" | bc -l)
  REGION_TOTAL=$(echo "$REGION_TOTAL + $EIP_COST" | bc -l)
  [ "$VERBOSE" = true ] && printf "  Elastic IP: \$%.4f\n" "$EIP_COST"

  # EBS
  EBS_GB=$(aws ec2 describe-volumes \
    --region $region \
    --query "Volumes[].Size" \
    --output text | awk '{s+=$1} END {print s}')

  if [ -z "$EBS_GB" ]; then EBS_GB=0; fi

  EBS_COST=$(echo "$EBS_GB * $COST_EBS_GB" | bc -l)
  REGION_TOTAL=$(echo "$REGION_TOTAL + $EBS_COST" | bc -l)
  [ "$VERBOSE" = true ] && printf "  EBS Storage: \$%.4f\n" "$EBS_COST"

  PROJECTED=$(echo "$PROJECTED + $REGION_TOTAL" | bc -l)

  [ "$VERBOSE" = true ] && printf "  Region Total: \$%.4f\n" "$REGION_TOTAL"

  REGION_JSON="${REGION_JSON}
    \"$region\": {
      \"ec2\": $(printf "%.4f" $EC2_COST),
      \"nat\": $(printf "%.4f" $NAT_COST),
      \"eks\": $(printf "%.4f" $EKS_COST),
      \"rds\": $(printf "%.4f" $RDS_COST),
      \"eip\": $(printf "%.4f" $EIP_COST),
      \"ebs\": $(printf "%.4f" $EBS_COST),
      \"region_total\": $(printf "%.4f" $REGION_TOTAL)
    },"
done

TOTAL=$(echo "$ACTUAL + $PROJECTED" | bc -l)

echo ""
echo "--------------------------------------------------"
printf "Projected infrastructure exposure: \$%.4f\n" "$PROJECTED"
printf "TOTAL FINANCIAL RISK: \$%.4f\n" "$TOTAL"
echo "--------------------------------------------------"

STATUS="SAFE"

if (( $(echo "$TOTAL >= $MAX_EXPOSURE" | bc -l) )); then
  STATUS="BLOCKED"
  echo "🛑 HARD LIMIT BREACHED"
elif (( $(echo "$TOTAL >= 15" | bc -l) )); then
  STATUS="CRITICAL"
  echo "🚨 STAGE 3 ALERT"
elif (( $(echo "$TOTAL >= 10" | bc -l) )); then
  STATUS="WARNING"
  echo "⚠️ STAGE 2 ALERT"
elif (( $(echo "$TOTAL >= 5" | bc -l) )); then
  STATUS="NOTICE"
  echo "⚠️ STAGE 1 ALERT"
else
  echo "✅ Within safe financial range"
fi

# --------------------------------------------------
# 4️⃣ JSON Report
# --------------------------------------------------

cat > $REPORT_FILE <<EOF
{
  "timestamp": "$TIMESTAMP",
  "month_to_date_spend": $(printf "%.4f" $ACTUAL),
  "projected_infrastructure_exposure": $(printf "%.4f" $PROJECTED),
  "total_financial_risk": $(printf "%.4f" $TOTAL),
  "status": "$STATUS",
  "regions": {
    ${REGION_JSON%,}
  }
}
EOF

echo ""
echo "📄 Financial report generated: $REPORT_FILE"

[ "$STATUS" = "BLOCKED" ] && exit 1 || exit 0
