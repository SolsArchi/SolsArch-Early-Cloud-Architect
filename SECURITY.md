# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

- Use GitHub's private reporting: **Security, then Report a vulnerability** on the repository page
  (https://github.com/SolsArchi/SolsArch-Early-Cloud-Architect/security/advisories/new).
- Include what you ran, what you expected and what happened. A small repository that reproduces the problem is the most useful thing you can send.

We acknowledge a report within 5 working days and tell you what we plan to do. We credit reporters who want it.

## What counts

SolsArch reads a repository you give it and writes files (reports, diagrams, Terraform, scripts, pipelines). The repository is **not trusted**.
These are in scope:

- running or leaking something because of what a repository contains (a file name, a package name, a git setting, a document),
- a generated script, workflow or Terraform file that would run something the repository chose when you use it,
- a path that makes the tool write outside its output folder,
- a secret value printed, stored or sent anywhere,
- generated infrastructure that is less safe than it claims to be.

## How the tool is built to keep this safe

- Standard library only; no packages to install and no install scripts.
- No shell is used: every program the tool starts gets a list of arguments. Cloud credentials are not passed to the validation tools (Terraform, Trivy, Checkov and the like).
- Reading git history runs `git` with its configuration, hooks, external diff, text conversion and pager switched off, because the repository's own `.git/config` is untrusted.
- Web requests are https only, redirects stay https, and a credential is dropped if a redirect leaves the host it was meant for.
- Names and paths that come from a repository are slugged or quoted before they reach a shell command, a Terraform string or a file path; a generated path can never leave the output folder.
- Third-party GitHub Actions in generated pipelines are pinned to commits, and the pipeline downloads SolsArch at a release tag, not a moving branch.
- Secret values found in a repository are redacted in every output.

The tests in `tests/test_security.py` run these protections against a hostile repository.

## What it cannot do

It does not log in to your cloud, so it cannot see how your account is really configured. A clean report means nothing was found in the files read.
Read the generated Terraform and run `terraform plan` before `apply`.
