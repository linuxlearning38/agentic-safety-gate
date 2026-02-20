package infrastructure

default allow = false

allowed_instance_types := {
    "t3.micro",
    "t3.small",
    "t3.medium",
}

############################
# Violations
############################

violation[msg] if {
    input.instance_type == ""
    msg := "Instance type must be specified"
}

violation[msg] if {
    not input.instance_type in allowed_instance_types
    msg := sprintf("Instance type '%s' is not allowed", [input.instance_type])
}

violation[msg] if {
    some i
    input.security_groups[i].cidr == "0.0.0.0/0"
    msg := "Security group exposes SSH to the public internet"
}

############################
# Allow Rule
############################

allow if {
    count(violation) == 0
}
