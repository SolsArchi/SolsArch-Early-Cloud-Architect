package solsarch.policy

# Baseline rules, evaluated by Open Policy Agent over `terraform show -json <plan>`. Each `deny` message is shown to the person; a plan with any is blocked.
# Rules only look at what the plan would create or update (`change.after`), and say the same thing for every cloud.

import rego.v1

creates_or_updates contains rc if {
    some rc in input.resource_changes
    some action in rc.change.actions
    action in {"create", "update"}
}

after(rc) := object.get(rc.change, "after", {})

# ---- network
open_cidr(cidrs) if {
    some c in cidrs
    c in {"0.0.0.0/0", "::/0"}
}

sensitive_port(from_port, to_port) if {
    some p in {22, 3389, 5432, 3306, 1433, 27017, 6379}
    from_port <= p
    p <= to_port
}

deny contains msg if {
    some rc in creates_or_updates
    rc.type == "aws_security_group_rule"
    a := after(rc)
    a.type == "ingress"
    open_cidr(object.get(a, "cidr_blocks", []))
    sensitive_port(a.from_port, a.to_port)
    msg := sprintf("%s opens port %d-%d to the whole internet", [rc.address, a.from_port, a.to_port])
}

deny contains msg if {
    some rc in creates_or_updates
    rc.type == "aws_security_group"
    some ing in object.get(after(rc), "ingress", [])
    open_cidr(object.get(ing, "cidr_blocks", []))
    sensitive_port(ing.from_port, ing.to_port)
    msg := sprintf("%s opens port %d-%d to the whole internet", [rc.address, ing.from_port, ing.to_port])
}

# ---- data stores
deny contains msg if {
    some rc in creates_or_updates
    rc.type in {"aws_db_instance", "aws_rds_cluster"}
    object.get(after(rc), "publicly_accessible", false) == true
    msg := sprintf("%s makes a database reachable from the internet", [rc.address])
}

deny contains msg if {
    some rc in creates_or_updates
    rc.type in {"aws_db_instance", "aws_rds_cluster"}
    object.get(after(rc), "storage_encrypted", true) == false
    msg := sprintf("%s stores data without encryption", [rc.address])
}

deny contains msg if {
    some rc in creates_or_updates
    rc.type == "aws_s3_bucket_public_access_block"
    a := after(rc)
    some key in {"block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"}
    object.get(a, key, true) == false
    msg := sprintf("%s allows public access to a storage bucket (%s is off)", [rc.address, key])
}

deny contains msg if {
    some rc in creates_or_updates
    rc.type == "azurerm_storage_account"
    object.get(after(rc), "allow_nested_items_to_be_public", false) == true
    msg := sprintf("%s lets storage containers be made public", [rc.address])
}

deny contains msg if {
    some rc in creates_or_updates
    rc.type in {"google_storage_bucket_iam_member", "google_storage_bucket_iam_binding"}
    a := after(rc)
    some who in array.concat([object.get(a, "member", "")], object.get(a, "members", []))
    who in {"allUsers", "allAuthenticatedUsers"}
    msg := sprintf("%s grants a storage bucket to everyone (%s)", [rc.address, who])
}

# ---- permissions
star(x) if x == "*"

star(x) if {
    is_array(x)
    "*" in x
}

deny contains msg if {
    some rc in creates_or_updates
    rc.type in {"aws_iam_policy", "aws_iam_role_policy", "aws_iam_user_policy", "aws_iam_group_policy"}
    doc := json.unmarshal(after(rc).policy)
    some st in statements(doc)
    st.Effect == "Allow"
    star(st.Action)
    star(st.Resource)
    msg := sprintf("%s allows every action on every resource", [rc.address])
}

statements(doc) := doc.Statement if is_array(doc.Statement)

statements(doc) := [doc.Statement] if is_object(doc.Statement)
