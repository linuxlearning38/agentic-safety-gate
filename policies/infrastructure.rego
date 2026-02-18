package infrastructure

default allow = false

allowed_instance_types := {
    "t3.micro",
    "t3.small",
    "t3.medium",
}

instance_type_allowed if {
    input.instance_type != ""
    input.instance_type in allowed_instance_types
}

ssh_not_public if {
    some i
    input.security_groups[i].cidr != "0.0.0.0/0"
}

allow if {
    instance_type_allowed
    ssh_not_public
}

