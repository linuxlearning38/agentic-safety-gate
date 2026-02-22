package infrastructure

default allow = false

############################
# Allowed Instance Types
############################

allowed_instance_types := {
    "t3.micro",
    "t3.small",
    "t3.medium",
}

############################
# Approved Regions
############################

approved_regions := {
    "ap-south-1",
    "eu-north-1",
}

############################
# Required Tags
############################

required_tags := {
    "Environment",
    "Owner",
    "CostCenter",
}

############################
# Violations
############################

# 1️⃣ Instance type must be specified
violation[msg] if {
    input.instance_type == ""
    msg := "Instance type must be specified"
}

# 2️⃣ Instance type must be allowed
violation[msg] if {
    not input.instance_type in allowed_instance_types
    msg := sprintf("Instance type '%s' is not allowed", [input.instance_type])
}

# 3️⃣ No public SSH exposure
violation[msg] if {
    some i
    input.security_groups[i].cidr == "0.0.0.0/0"
    msg := "Security group exposes SSH to the public internet"
}

# 4️⃣ Required tags must exist
violation[msg] if {
    some tag
    required_tags[tag]
    not input.tags[tag]
    msg := sprintf("Missing required tag: %s", [tag])
}

# 5️⃣ Region must be approved
violation[msg] if {
    not input.region in approved_regions
    msg := sprintf("Region '%s' is not approved", [input.region])
}

# 6️⃣ Encryption must be enabled
violation[msg] if {
    input.encrypted == false
    msg := "Storage must be encrypted"
}

# 7️⃣ No public S3 buckets
violation[msg] if {
    input.s3_bucket
    input.s3_bucket.public_access == true
    msg := sprintf("S3 bucket '%s' cannot be public", [input.s3_bucket.name])
}

# 8️⃣ No IAM wildcard actions
violation[msg] if {
    input.iam_policy
    some stmt in input.iam_policy.statements
    stmt.effect == "Allow"
    some action in stmt.actions
    action == "*"
    msg := "IAM policy contains wildcard action '*'"
}

# 9️⃣ No public RDS instances
violation[msg] if {
    input.rds_instance
    input.rds_instance.publicly_accessible == true
    msg := sprintf("RDS instance '%s' cannot be publicly accessible", [input.rds_instance.name])
}

# 🔟 Cost guardrail enforcement
violation[msg] if {
    input.estimated_cost_usd
    input.cost_limit_usd
    input.estimated_cost_usd > input.cost_limit_usd
    msg := sprintf(
        "Estimated cost $%.2f exceeds limit $%.2f",
        [input.estimated_cost_usd, input.cost_limit_usd]
    )
}

############################
# Allow Rule
############################

allow if {
    count(violation) == 0
}
