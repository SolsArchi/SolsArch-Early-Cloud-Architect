#!/usr/bin/env python3
"""A step-by-step deployment guide written for a person who has never deployed anything.

    deploy_guide.build(plan, design, profile, manifest) -> markdown text

The guide is a pure function of the plan, the design review, the profile (including the account type: free trial, always-free
tier, pay as you go, or standard paid) and the list of files the infrastructure generator wrote. It gives two ways in for every
provider: copy-and-paste terminal commands, and clicks in the provider's web console. Each step says what you should see and
what to do if you do not. It never quotes a price; amounts and free-offer terms change, so it says where to read them.
"""
import json
from pathlib import Path

from iac_common import IMAGE_TAG, shq

REF = Path(__file__).resolve().parent.parent / "references" / "deploy-guides.json"
FOLDER = "infrastructure"

ACCOUNT_LABELS = {
    "free_trial": "Free trial (credits for a limited time)",
    "free_tier": "Always-free tier (small monthly limits, no credit)",
    "pay_as_you_go": "Pay as you go (a card on file, billed for what you use)",
    "standard": "Standard paid account (company or team)",
}

PLATFORM_FLOW = {"render", "flyio", "railway", "heroku", "vercel", "netlify", "cloudflare", "denodeploy", "fastly", "firebase", "supabase",
                 "databricks", "snowflake", "runpod", "togetherai", "lambdalabs", "huggingface", "cloudfoundry", "koyeb"}
MAJOR = {"aws", "azure", "gcp"}
TWO_PHASE = {"digitalocean", "scaleway", "oci", "alibaba", "huawei", "ibm"}
K8S = {"linode", "akamai", "vultr", "ovhcloud", "civo", "upcloud", "exoscale"}
DATA = {"neon", "mongodbatlas"}
PLATFORM_FOLDER = {"render": "render", "flyio": "fly", "railway": "railway", "heroku": "heroku", "vercel": "vercel", "netlify": "netlify",
                   "cloudflare": "cloudflare", "denodeploy": "deno", "fastly": "fastly", "firebase": "firebase", "supabase": "supabase",
                   "databricks": "databricks", "snowflake": "snowflake", "runpod": "runpod", "togetherai": "together", "lambdalabs": "lambdalabs",
                   "coreweave": "coreweave", "huggingface": "huggingface", "cloudfoundry": "cloudfoundry", "koyeb": "koyeb"}

GLOSSARY = [
    ("Cloud account", "Your login with a company that rents out computers (AWS, Azure, Google and others). It holds your bill."),
    ("Console", "The website where you click around to see and manage what you rent."),
    ("Terminal", "A window where you type commands. On Windows open PowerShell; on a Mac open Terminal."),
    ("Terraform", "A tool that reads the files in this folder and builds everything for you in one go. It can also remove everything in one go."),
    ("Container / image", "Your application packed in a box that runs the same anywhere. An image is the box; the registry is the shelf it sits on."),
    ("Registry", "A private shelf in your cloud where your application images are stored."),
    ("Region", "The place in the world where your things run. Pick one near your users."),
    ("Secret", "A password or key. Never put one in a file you share or commit."),
    ("State", "Terraform's notebook of what it built. Keep it in the bucket this guide creates, not on your laptop."),
    ("Budget alert", "An email that warns you when spending passes an amount you choose."),
]


def load():
    return json.loads(REF.read_text(encoding="utf-8"))


def resolve(data, provider):
    entry = data["providers"].get(provider) or {}
    if "alias" in entry:
        entry = data["providers"][entry["alias"]]
    return entry


def block(code, lang="bash"):
    return "```%s\n%s\n```" % (lang, code.strip("\n"))


def setenv(name, value):
    return "macOS / Linux:\n" + block("export %s=%s" % (name, value)) + "\nWindows PowerShell:\n" + block('$env:%s = "%s"' % (name, value), "powershell")


def numbered(steps):
    out = []
    for i, step in enumerate(steps, 1):
        out.append("%d. %s" % (i, step))
    return "\n".join(out)


def expect(text):
    return "> **You should see:** " + text + "\n"


def fails(text):
    return "> **If it fails:** " + text + "\n"


def tool_lines(data, names):
    lines = ["| Tool | Windows (PowerShell) | macOS (Terminal) | Linux | Check it worked |", "|---|---|---|---|---|"]
    for n in names:
        t = data["tools"][n]
        lines.append("| %s | `%s` | `%s` | %s | `%s` |" % (n.capitalize(), t["windows"], t["mac"], t["linux"], t["verify"]))
    return "\n".join(lines)


def provider_cli_row(entry):
    cli = entry.get("cli") or {}
    inst = cli.get("install") or {}
    if not inst or inst.get("windows") == "Not needed":
        return ""
    return "| %s | `%s` | `%s` | %s | `%s` |" % (cli.get("name", ""), inst.get("windows", ""), inst.get("mac", ""), inst.get("linux", ""), cli.get("whoami") or cli.get("name", ""))


def account_section(account, entry, name, lean):
    free = (entry.get("free") or {}).get(account, "")
    out = ["## Step 1. Get your %s account ready" % name, "",
           "**Your account type:** %s" % ACCOUNT_LABELS.get(account, account), "",
           free, ""]
    if lean:
        out += ["Because your account is %s, SolsArch chose the **smallest correct design**: one availability zone, one outbound path, and nothing the code does not use. "
                "That keeps it inside the limits. Production needs more (a second zone, a CDN, backups you have tested); the report lists exactly what to add later." % ACCOUNT_LABELS[account].split(" (")[0].lower(), ""]
    steps = ["Open %s and create the account (or sign in if you have one)." % entry.get("signup", "the provider's sign-up page")]
    if account == "free_trial":
        steps += ["Write down the **date your trial ends** and the **credit amount** shown in the billing page. Put a reminder in your calendar a week before.",
                  "Some sizes and services are blocked on trial accounts. If a step later says a limit was reached, that is why: ask support to raise it or upgrade the account.",
                  "Before the trial ends, either delete everything (see the last step) or upgrade on purpose. Do not let it lapse with things still running."]
    elif account == "free_tier":
        steps += ["Read the list of always-free limits on the provider's pricing page and keep to them. Anything outside it costs money.",
                  "Set a budget alert at the **smallest amount** the provider allows (see Step 2), so you hear about the first charge.",
                  "Do not turn on extras this guide did not ask for (extra zones, bigger sizes, backups)."]
    elif account == "pay_as_you_go":
        steps += ["Add a payment card when asked. You are billed for what runs, usually monthly.",
                  "Set budget alerts (Step 2) before you build anything.",
                  "If a later step says a quota or limit was reached, open the provider's quota page and ask for an increase; it can take a few hours to a day."]
    else:
        steps += ["Use a **separate account, subscription or project for production**, not the one you experiment in.",
                  "Turn on multi-factor sign-in for every person, and give people the least access they need. Do not share the owner login.",
                  "Agree who approves changes. The generated pipeline already asks for a person to approve before it applies anything."]
    steps.append("Turn on **multi-factor sign-in** (a code from your phone) for the owner login. It is the single best protection for a cloud account.")
    out.append(numbered(steps))
    out.append("")
    return out


def budget_section(entry, account):
    out = ["## Step 2. Set a spending alert before you build anything", "",
           "This takes five minutes and stops a surprise bill.", ""]
    out.append(numbered(entry.get("budget_console") or ["Open the provider's billing page and look for budgets or alerts."]))
    out.append("")
    amount = {"free_trial": "about half of your trial credit", "free_tier": "the smallest amount the provider allows", "pay_as_you_go": "the most you are happy to pay in a month",
              "standard": "the monthly figure your finance team agreed"}.get(account, "an amount you choose")
    out += ["Suggested amount: **%s**. Add alerts at 50%%, 80%% and 100%% if you can." % amount, "",
            "The expected monthly cost is in `docs/solsarch-costs.md`. It shows which prices were read live from the provider and which are estimates.", ""]
    return out


def tools_section(data, entry, names, name):
    out = ["## Step 3. Install the tools on your computer", "",
           "You only do this once. Open a terminal (Windows: search for **PowerShell**; Mac: open **Terminal**) and paste the line for your system.", "",
           tool_lines(data, names)]
    row = provider_cli_row(entry)
    if row:
        out += ["", "And the %s tool:" % name, "", "| Tool | Windows (PowerShell) | macOS (Terminal) | Linux | Check it worked |", "|---|---|---|---|---|", row]
    out += ["", expect("a version number for each tool, not an error."),
            fails("close the terminal, open a new one (the computer needs a fresh window to find new tools), and try the check again. On Windows, `winget` comes with Windows 10 and later; if it is missing, install **App Installer** from the Microsoft Store."), ""]
    return out


def credentials_section(entry, name, env_note):
    out = ["## Step 4. Let your computer talk to %s" % name, ""]
    steps = entry.get("credential_console") or ["Create an access token in the provider's console."]
    out += ["**In the web console (clicks):**", "", numbered(steps), ""]
    cli = entry.get("cli") or {}
    if cli.get("login"):
        out += ["**In the terminal:**", "", block(cli["login"])]
        if cli.get("whoami"):
            out += ["Then check you are signed in:", "", block(cli["whoami"]), expect("your account or project name, not an error.")]
    if cli.get("env"):
        out += ["Terraform reads these settings from your terminal session (they are not saved in any file): `%s`." % cli["env"], ""]
        first = cli["env"].split(",")[0].strip()
        out += [setenv(first, "paste-the-value-here"), "Set each one the same way. Close the terminal when you finish so they are forgotten.", ""]
    if env_note:
        out += [env_note, ""]
    out += ["> **Safety:** treat the key like a password. Do not paste it into chat, email or a file inside the project. If you leak it, delete it in the console and make a new one.", ""]
    return out


def unit_lines(design):
    return ["- **%s** (%s) %s%s" % (u["name"], u["kind"], ("listens on port %s" % u["port"]) if u["port"] else "has no port (it works in the background)", ", health check `%s`" % u["health_path"] if u.get("health_path") else "")
            for u in design["units"] if u["kind"] != "static"]


def image_commands(design, entry, provider, registry_hint):
    lines = []
    for u in design["units"]:
        if u["kind"] == "static":
            continue
        folder = u["folder"].strip("./") or "."
        docker = u.get("dockerfile") or ("%s/docker/%s/Dockerfile" % (FOLDER, u["name"]))
        lines.append("docker build -f %s -t %s/%s:%s %s" % (shq(docker), registry_hint, u["name"], IMAGE_TAG, shq(folder)))
        lines.append("docker push %s/%s:%s" % (registry_hint, u["name"], IMAGE_TAG))
    return "\n".join(lines)


def apply_flow(provider, entry, name, design, manifest, account):
    out = ["## Step 5. Build everything (terminal path)", "",
           "Open the terminal in the folder you unzipped, then go into the generated folder:", "", block("cd %s/terraform" % FOLDER), ""]
    if provider in MAJOR:
        first = {"aws": ("cd bootstrap\nterraform init\nterraform apply\ncd ..", "It makes a private bucket where Terraform keeps its notebook (the state)."),
                 "azure": ("az login\ncd bootstrap\nterraform init\nterraform apply\ncd ..", "It makes a storage account where Terraform keeps its notebook (the state)."),
                 "gcp": ("gcloud auth application-default login\ncd bootstrap\nterraform init\nterraform apply -var project_id=YOUR_PROJECT_ID\ncd ..", "It makes a bucket where Terraform keeps its notebook (the state).")}[provider]
        out += ["**5a. One-time setup.** " + first[1], "", block(first[0]), expect("`Apply complete!` and a few values listed (a bucket or account name). Terraform asks `Do you want to perform these actions?` Type `yes`."),
                fails("an error that mentions credentials means Step 4 did not finish: run the sign-in check again. An error that mentions a name already taken means someone else owns that name: add a few random letters to `project_name` in `terraform.tfvars` and retry."), "",
                "**5b. Tell Terraform where its notebook is.**", "", block("cp backend.hcl.example backend.hcl", "bash"),
                "Open `backend.hcl` in any text editor (Notepad is fine) and paste the bucket or storage account name that 5a printed. Save it.", "",
                "**5c. Fill in your settings.**", "", "Copy `terraform.tfvars.example` to `terraform.tfvars`, open it, and change the values it marks REPLACE.", ""]
        apply_cmd = "terraform init -backend-config=backend.hcl\nterraform plan\nterraform apply" + (" -var project_id=YOUR_PROJECT_ID" if provider == "gcp" else "")
        first_half = "the networks, databases and the shelf for your images"
        if provider == "aws" and manifest.get("compute_model") == "serverless":
            # A function cannot be created before its image exists, so the first apply makes only the image repositories.
            from iac_common import ident
            targets = " ".join("-target=aws_ecr_repository.%s" % ident(u["name"]) for u in design["units"] if u["kind"] != "static")
            apply_cmd = chr(10).join(["terraform init -backend-config=backend.hcl", "terraform plan " + targets, "terraform apply " + targets])
            first_half = "only the shelves (image repositories) your functions will be built from"
        out += ["**5d. Build the first half** (%s)." % first_half, "", block(apply_cmd),
                expect("`plan` lists what will be created and ends with `Plan: N to add, 0 to change, 0 to destroy`. After you type `yes` for `apply`, it runs for 10 to 30 minutes and ends with `Apply complete!`. The databases take the longest."),
                fails("if it stops with `quota` or `limit`, see the note in Step 1 for your account type. If it stops part-way, run `terraform apply` again; it carries on where it stopped."), ""]
        out += ["**5e. Put your application on the shelf (the registry).**", "",
                "Make sure Docker Desktop is running (a whale icon appears). Then sign Docker in to your registry and push each unit:", ""]
        reg = {"aws": "REPOSITORY_URL_FROM_TERRAFORM_OUTPUT", "azure": "REGISTRY_LOGIN_SERVER", "gcp": "REGION-docker.pkg.dev/PROJECT/REPOSITORY"}[provider]
        out += [block(entry.get("cli_for_images", "")), "Print the registry address with `terraform output`, then:", "", block(image_commands(design, entry, provider, reg)),
                expect("`pushed` lines ending in a digest (a long code). It can take several minutes for the first push."),
                ("**Version numbers.** The registry never overwrites a version, so each new build needs a new number. The first push uses `%s`. To deploy a change, "
                 "build and push with the next number (for example `0.1.1`) and run `terraform apply -var image_tag=0.1.1`." % IMAGE_TAG) if provider == "aws" else "",
                fails("`unauthorized` means the sign-in line above expired: run it again."), ""]
        out += ["**5f. Start the application.**", "", block("terraform apply" + (" -var project_id=YOUR_PROJECT_ID" if provider == "gcp" else "")),
                expect("`Apply complete!` again. The services now start from the images you pushed."), ""]
    elif provider in TWO_PHASE:
        out += ["Terraform builds in two halves so that nothing waits for an image that does not exist yet.", "",
                "**5a. Tell Terraform where its notebook is.** Create a storage bucket in the console for it (the README in `terraform/` names the type), copy `backend.hcl.example` to `backend.hcl`, and fill it in.", "",
                "**5b. Fill in your settings.** Copy `terraform.tfvars.example` to `terraform.tfvars` and change the values marked REPLACE.", "",
                "**5c. Build the first half** (`deploy_app` stays `false`):", "", block("terraform init -backend-config=backend.hcl\nterraform plan\nterraform apply"),
                expect("`Plan: N to add`, then after `yes`, `Apply complete!`. Databases can take 15 to 30 minutes."),
                fails("an error naming a plan, size or version means the provider changed its list: open `terraform/README.md` (the Notes section) and change that one value; then run `terraform apply` again."), "",
                "**5d. Put your application on the shelf (the registry).** Make sure Docker Desktop is running.", "",
                block(entry.get("cli_for_images", "")), "Get the registry address from `terraform output registry_address` (or `registry_endpoint`), then:", "",
                block(image_commands(design, entry, provider, "REGISTRY_ADDRESS")), expect("`pushed` lines for each unit."), "",
                "**5e. Start the application.** Open `terraform.tfvars`, change `deploy_app = false` to `deploy_app = true`, save, then:", "", block("terraform apply"),
                expect("`Apply complete!` and the address of your application in the output."), ""]
    elif provider in K8S or provider == "tencent":
        out += ["This design runs on **Kubernetes**, a system that keeps your containers running. Terraform creates the cluster and the databases; then you hand it your application.", "",
                "**5a.** Create the storage bucket for Terraform's notebook, copy `backend.hcl.example` to `backend.hcl`, and fill it in.", "",
                "**5b.** Copy `terraform.tfvars.example` to `terraform.tfvars` and change the values marked REPLACE.", "",
                "**5c. Build the cluster and databases:**", "", block("terraform init -backend-config=backend.hcl\nterraform plan\nterraform apply"),
                expect("`Apply complete!` after 10 to 30 minutes."), "",
                "**5d. Get the key to the cluster:**", "", block("terraform output -raw kubeconfig | base64 -d > kubeconfig.yaml\nexport KUBECONFIG=$PWD/kubeconfig.yaml\nkubectl get nodes"),
                "On Windows PowerShell use: `$env:KUBECONFIG = \"$PWD\\kubeconfig.yaml\"`.",
                expect("one or more lines with status `Ready`."), fails("`Unable to connect` usually means the cluster is still starting: wait five minutes and try again."), "",
                "**5e. Put your application on the shelf (the registry), then install the add-ons and the application:**", "",
                block(image_commands(design, entry, provider, "REGISTRY_ADDRESS")), "Then edit `../kubernetes/configmap.yaml` (the database address from `terraform output`) and create the secret as `../kubernetes/README.md` shows. Finally:", "",
                block("cd ../ansible\nansible-galaxy collection install -r requirements.yml\nansible-playbook k8s-addons.yml"),
                expect("a recap ending in `failed=0`. The application is now running."), ""]
    elif provider == "hetzner":
        out += ["Hetzner rents plain servers, so Terraform creates the servers and network, and Ansible then sets up the software on them.", "",
                "**5a. Make a login key for the servers** (skip if you already have one):", "", block("ssh-keygen -t ed25519 -C \"you@example.com\""),
                "macOS / Linux: `cat ~/.ssh/id_ed25519.pub`. Windows PowerShell: `type $env:USERPROFILE\\.ssh\\id_ed25519.pub`. Copy that line into `ssh_public_key` in `terraform.tfvars`.", "",
                "**5b.** Create a bucket for Terraform's notebook in the Hetzner console (Object Storage), copy `backend.hcl.example` to `backend.hcl`, and fill it in.", "",
                "**5c. Create the servers:**", "", block("cp terraform.tfvars.example terraform.tfvars\nterraform init -backend-config=backend.hcl\nterraform plan\nterraform apply"),
                expect("`Apply complete!` and an `ansible_inventory` output with the server addresses."), "",
                "**5d. Put your application on a registry the servers can pull from.** Hetzner has no registry of its own. Use a free private one (for example GitHub Container Registry, `ghcr.io`, or Docker Hub), then set `registry_user` and `units[].image` in `ansible/group_vars/all/main.yml` and the registry password in the vault file:", "",
                block(image_commands(design, entry, provider, "ghcr.io/YOUR_NAME/YOUR_PROJECT")), "",
                "**5e. Set up the software:**", "", block("terraform output -raw ansible_inventory > ../ansible/inventory/production.ini\ncd ../ansible\nansible-galaxy collection install -r requirements.yml\ncp group_vars/all/vault.example.yml group_vars/all/vault.yml\nansible-vault encrypt group_vars/all/vault.yml\nansible-playbook site.yml --check --diff\nansible-playbook site.yml --ask-vault-pass"),
                "Edit `group_vars/all/vault.yml` (before encrypting) to put real passwords in. Ansible runs on Mac, Linux, or Windows inside WSL.",
                expect("a recap ending in `failed=0`, with the health check passing at the end."), ""]
    return out


def platform_flow(provider, entry, name, files):
    folder = PLATFORM_FOLDER.get(provider, provider)
    readme = "%s/%s/README.md" % (FOLDER, folder)
    out = ["## Step 5. Deploy (terminal path)", "",
           "%s is deployed with its own configuration file, not Terraform. Everything it needs is in `%s/%s/`." % (name, FOLDER, folder), "",
           "Open `%s` in any text editor and follow it; the same steps are repeated here in plain words." % readme, ""]
    steps = []
    for line in (files.get("%s/README.md" % folder, "") or "").splitlines():
        head, _, rest = line.partition(". ")
        if head.isdigit() and rest:
            steps.append(rest)
    if steps:
        out += [numbered(steps), ""]
    if "%s/deploy.sh" % folder in files:
        out += ["The file `%s/%s/deploy.sh` runs the commands in order. Read it first, then run:" % (FOLDER, folder), "", block("bash %s/%s/deploy.sh" % (FOLDER, folder)),
                "On Windows run it inside WSL or Git Bash.", ""]
    out += [expect("the platform prints the address of your application when the deploy finishes."),
            fails("read the last lines the command printed; they name the problem. The most common causes are not being signed in (Step 4) and a missing secret (set it with the platform's secrets command or dashboard)."), ""]
    return out


def data_flow(provider, entry, name):
    folder = {"neon": "terraform-neon", "mongodbatlas": "terraform-mongodbatlas"}[provider]
    return ["## Step 5. Create the database (terminal path)", "", block("cd %s/%s\nterraform init\nterraform plan\nterraform apply" % (FOLDER, folder)),
            expect("`Apply complete!`. The connection details are in `terraform output` (the password is hidden; use `terraform output -raw database_password`)."), ""]


def console_section(provider, entry, name, plan, kind):
    out = ["## Step 5 (alternative). Build it by clicking in the console", "",
           "Use this if you would rather not use a terminal. It takes longer and is easier to get wrong, so the terminal path above is recommended where it exists.", ""]
    out += [numbered(entry.get("console_deploy") or ["Open the provider's console and create each service listed below."]), ""]
    out += ["**The services to create** (this is what SolsArch planned for you):", ""]
    for d in plan["decisions"]:
        out.append("- **%s**: %s. %s" % (d["role"], d["service"], d.get("why", "").split(". ")[-1][:140]))
    out += ["", "Open `%s/terraform/README.md` (or the platform folder's README) and match each setting it lists under *What is configured for you*; those are the safe settings (private network, encryption, health checks).", ""]
    return out


def verify_section(design, provider, kind):
    out = ["## Step 6. Check that it works", ""]
    pubs = [u for u in design["units"] if u["kind"] in ("web", "api", "service")]
    out += ["1. Find your address. In the terminal run `terraform output` (or look in the dashboard / the message the deploy printed).",
            "2. Open the address in a browser. You should see your application."]
    for u in pubs:
        if u.get("health_path"):
            out.append("3. Open `ADDRESS%s` for **%s**. You should see a short OK message or `200`." % (u["health_path"], u["name"]))
            break
    out += ["4. Try signing up or saving something. If it works, the database is connected.",
            "5. Look at the logs once (the console's *Logs* page for the service) to confirm there are no red errors.", "",
            fails("a blank page or `502`/`503` usually means the service is still starting (wait two minutes) or a secret is missing. Check the logs for the first red line."), ""]
    return out


def domain_section(provider, kind):
    return ["## Step 7. Your own web address and the padlock (HTTPS)", "",
            "You can use the address the provider gave you, or your own domain (for example `www.yourcompany.com`).", "",
            "1. Buy a domain from any registrar, or use one you own.",
            "2. In the registrar's DNS page, add the record the deploy output tells you to (an **A** record to an IP address, or a **CNAME** to a name).",
            "3. Put the domain in `terraform.tfvars` (the line called `domain` or `domain_name`) or in the platform's *Custom domain* page, then apply or save again.",
            "4. Wait up to an hour. The padlock appears when the certificate is issued.", "",
            "> Until the certificate is set up the application may answer on plain HTTP. Do not send real customer data over it.", ""]


def cleanup_section(entry, name, account):
    out = ["## Step 8. Turn it off when you are done", "",
           "Things you leave running keep costing money, even if nobody uses them.", "",
           block(entry.get("teardown", "Delete each service in the provider's console.")), ""]
    if account in ("free_trial", "free_tier"):
        out += ["Because you are on a %s account, do this **before the free period or limit ends** unless you chose to keep it." % ACCOUNT_LABELS[account].split(" (")[0].lower(), ""]
    out += ["Then open the billing page and confirm the current charges stop growing.", ""]
    return out


def troubleshooting():
    rows = [
        ("`not authorized`, `access denied`, `invalid token`", "The sign-in from Step 4 is missing or expired. Sign in again and set the key in this same terminal window."),
        ("`quota exceeded`, `limit reached`, `not available in this region`", "Your account or region does not allow that size or service. Free and trial accounts have the lowest limits. Pick another region in `terraform.tfvars`, or ask the provider to raise the limit."),
        ("`name already exists`, `already taken`", "Names are shared worldwide for some things (buckets, registries). Add a few random letters to `project_name` in `terraform.tfvars`."),
        ("`Error acquiring the state lock`", "A previous run was interrupted. Wait five minutes. If it persists, run the unlock command Terraform prints."),
        ("`image not found`, `manifest unknown`", "The application image was not pushed yet, or the tag differs. Repeat the push step and check the tag in `terraform.tfvars`."),
        ("The site shows `502` or `503`", "The service is still starting, or it cannot reach the database. Wait two minutes, then read the service logs."),
        ("The terminal says a command is `not recognized` or `not found`", "Close the terminal and open a new one after installing tools. On Windows make sure you used PowerShell, not an old Command Prompt."),
        ("Docker says `Cannot connect to the Docker daemon`", "Open Docker Desktop and wait until it says it is running."),
        ("Terraform stops with a long red message about a size, plan or version", "Providers rename their sizes. Open the README in the `terraform` folder, Notes section, and change the one value it names."),
    ]
    lines = ["## If something goes wrong", "", "| You see | What to do |", "|---|---|"]
    lines += ["| %s | %s |" % r for r in rows]
    lines += ["", "If you are stuck, copy the **first red line** of the message (not the whole page) and search for it together with the provider's name, or send it to your team."]
    return lines


def build(plan, design, profile, manifest, files=None):
    data = load()
    provider = plan["cloud"]
    entry = resolve(data, provider)
    name = plan["cloud_name"]
    files = files or {}
    account = (profile or {}).get("account_type") or "standard"
    lean = account in ("free_trial", "free_tier")
    project = plan.get("project") or "your project"
    kind = manifest.get("provider_kind", "cloud")
    out = ["# How to deploy %s on %s, step by step" % (project, name), "",
           "_Prepared with SolsArch Early Cloud Architect for plan `%s`. Written for someone who has never deployed an application._" % plan["fingerprint"], "",
           "## What you will end up with", "",
           "- Your application running on **%s** (%s)." % (name, plan["compute_label"]),
           "- These parts, each with its job:"]
    out += ["  - %s: %s" % (d["role"], d["service"]) for d in plan["decisions"]]
    out += ["", "Your units:", ""] + unit_lines(design) + ["",
            "**Time:** about 1 to 2 hours the first time, most of it waiting for the cloud to finish.",
            "**You need:** a computer (Windows, Mac or Linux), an email address, %s, and your application's source code." % ("a payment card (the account may require one even for free use)" if account != "standard" else "a payment method or billing agreement"), "",
            "How to read this guide: each step says what to do, what you should see, and what to try if you do not. Words you may not know are explained at the end.", ""]
    out += account_section(account, entry, name, lean)
    out += budget_section(entry, account)
    names = ["git"] if provider in PLATFORM_FLOW or provider in DATA else ["git", "docker"]
    if provider in ("flyio", "heroku"):
        names.append("docker")
    if provider in MAJOR or provider in TWO_PHASE or provider in K8S or provider in DATA or provider in ("hetzner", "tencent"):
        names.insert(0, "terraform")
    if provider in K8S or provider in ("tencent", "coreweave"):
        names += ["kubectl", "ansible"]
    if provider == "hetzner":
        names.append("ansible")
    if provider in ("vercel", "netlify", "cloudflare", "firebase", "railway"):
        names.append("node")
    out += tools_section(data, entry, names, (entry.get("cli") or {}).get("name") or name)
    out += credentials_section(entry, name, "")
    if provider in MAJOR or provider in TWO_PHASE or provider in K8S or provider in ("tencent", "hetzner"):
        out += apply_flow(provider, entry, name, design, manifest, account)
    elif provider in DATA:
        out += data_flow(provider, entry, name)
    elif provider in PLATFORM_FLOW or provider == "coreweave":
        out += platform_flow(provider, entry, name, files)
    out += console_section(provider, entry, name, plan, kind)
    out += verify_section(design, provider, kind)
    out += domain_section(provider, kind)
    out += cleanup_section(entry, name, account)
    out += troubleshooting()
    out += ["", "## Words used in this guide", "", "| Word | Meaning |", "|---|---|"] + ["| %s | %s |" % g for g in GLOSSARY]
    out += ["", "---", "Generated by SolsArch Early Cloud Architect. Free-offer terms, sizes and menu names change; where this guide and the provider disagree, the provider is right."]
    return "\n".join(out) + "\n"
