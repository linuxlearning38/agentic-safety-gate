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

# Instance type must be specified
violation[msg] if {
    input.instance_type == ""
    msg := "Instance type must be specified"
}

# Instance type must be allowed
violation[msg] if {
    not input.instance_type in allowed_instance_types
    msg := sprintf("Instance type '%s' is not allowed", [input.instance_type])
}

# Security group must not expose SSH publicly
violation[msg] if {
    some i
    input.security_groups[i].cidr == "0.0.0.0/0"
    msg := "Security group exposes SSH to the public internet"
}

# Required tags must exist
violation[msg] if {
    some tag
    required_tags[tag]
    not input.tags[tag]
    msg := sprintf("Missing required tag: %s", [tag])
}

# Region must be approved
violation[msg] if {
    not input.region in approved_regions
    msg := sprintf("Region '%s' is not approved", [input.region])
}

# Encryption must be enabled
violation[msg] if {
    input.encrypted == false
    msg := "Storage must be encrypted"
}

############################
# Allow Rule
############################

allow if {
    count(violation) == 0
}
