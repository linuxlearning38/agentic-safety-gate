package ava.authz

default effect := "block"
default reason := "No AVA action policy matched"

destructive_command if {
    command := lower(input.command)
    contains(command, "rm -rf")
}

destructive_command if {
    command := lower(input.command)
    contains(command, "drop table")
}

destructive_command if {
    command := lower(input.command)
    contains(command, "truncate table")
}

destructive_command if {
    command := lower(input.command)
    contains(command, "delete database")
}

destructive_command if {
    command := lower(input.command)
    contains(command, "shutdown")
}

shell_chain if {
    contains(input.command, "&&")
}

shell_chain if {
    contains(input.command, "||")
}

shell_chain if {
    contains(input.command, ";")
}

block if {
    input.risk == "critical"
}

block if {
    destructive_command
}

block if {
    input.mode == "command"
    shell_chain
}

effect := "block" if {
    block
}

reason := "Action blocked by OPA policy" if {
    block
}

effect := "allow" if {
    not block
    input.risk == "low"
}

reason := "Low-risk action allowed by OPA policy" if {
    not block
    input.risk == "low"
}

effect := "require_approval" if {
    not block
    input.risk == "medium"
}

reason := "Medium-risk action requires approval by OPA policy" if {
    not block
    input.risk == "medium"
}

effect := "require_approval" if {
    not block
    input.risk == "high"
}

reason := "High-risk action requires approval by OPA policy" if {
    not block
    input.risk == "high"
}

decision := {
    "effect": effect,
    "reason": reason,
    "policy_id": "ava_action_policy_v1",
}
