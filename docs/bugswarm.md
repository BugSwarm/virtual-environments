# BugSwarm GitHub Actions Images

BugSwarm uses stripped-down versions of GitHub's Ubuntu runner images, converted from Azure VM images to Docker images.
Only a small amount of the original software is included, since the full images can weigh in at over 60 GB.
They are also updated much less frequently.

To create new versions of these Docker images, follow the follwing steps:

1. Install [Packer][1] if it's not already installed.
1. Pull in updates from the upstream repo by clicking the "Sync Fork" button on the repo page.
1. Create a new branch to make changes in.
1. Make whatever changes are necessary to the [Packer HCL templates](#editing-packer-templates), [toolset JSON files](#editing-the-toolset-json), and [install scripts](#editing-the-install-scripts).
   - This will involve removing much of the software installed by Packer.
1. Build the new Docker images using `packer build`.

**Tip:** To get a sense of how much space each step consumes, you can add the following line to the end of `images/ubuntu/scripts/build/*.sh` to get the size of the image (in megabytes):

```sh
du -s -BM / || true
```

## Editing Packer Templates

The Packer templates, located at `images/ubuntu/templates/ubuntu-<version>.pkr.hcl`, tell Packer how to build the runner image.

For full documentation on Packer HCL templates, click [here][2].
For an example of the kinds of changes to make, see [this diff][3].

### Convert to Docker

At the top of the file is the `required_plugins` block.
Remove the `azure` object and replace it with a `docker` object, like so:

```hcl
packer {
  required_plugins {
    docker = {
      source = "github.com/hashicorp/docker"
      version = ">= 1.0.8"
    }
  }
}
```

Farther down is a block labeled `source "azure-arm" "build_image"`.
This block defines the configuration for the builder.
Since we're building Docker images instead of Azure images, delete the whole block and replace it with the following:

```hcl
source "docker" "build_image" {
  image  = "<base image>"  // Either "ubuntu:20.04" or "ubuntu:22.04"
  commit = true            // Commit a new image at the end of the build
  // Make the following changes at the end of the build
  changes = [
    "ENV LC_ALL=C.UTF-8",
    "ENV LANG=C.UTF-8",
    "ENV LANGUAGE=C.UTF-8"
  ]
}
```

At the top of the `build` section, make the following changes so Packer builds a Docker image and tags it correctly.

```diff
build {
-  sources = ["source.azure-arm.build_image"]
+  sources = ["source.docker.build_image"]
+
+  post-processor "docker-tag" {
+    repository = "bugswarm/githubactionsjobrunners"
+    tags = ["<year>.<month>.ubuntu-<version>"]
+  }
```

Just after this, still in the `build` block, add the following provision step so essential commands like `sudo` are available later in the build.

```hcl
// Install basic commands (e.g. sudo)
provisioner "shell" {
  execute_command = "sh -c '{{ .Vars }} {{ .Path }}'"
  inline          = ["apt-get -y update", "apt-get -y install lsb-release sudo rsync curl wget apt-utils"]
}
```

### Change the Software List

Large sections of the Packer templates are devoted to running scripts to install various software.
These scripts are named `install-*.sh` or `Install-*.ps1`.
Most of this software is not used by the repos we're interested in (Java and Python), and if we left them in the base images would be enormous, so you should remove most of it.
Only leave software that is either essential for GitHub Actions to run or is likely to be used by the jobs we try to reproduce.

There are a few categories of software that you should leave in.

- Git and related software (`install-git.sh`, `install-git-lfs.sh`, `install-github-cli.sh`)
- Node and related, needed to run actions (`install-nodejs.sh`, `install-nvm.sh`)
- Java and related (`install-java-tools.sh`, `install-kotlin.sh`)
- Python and related (`install-python.sh`, `install-pypy.sh`, `install-miniconda.sh`, `install-pipx-packages.sh`)
- Software needed for GHA to work, or used later in the build (`install-ms-repos.sh`, `install-actions-cache.sh`, `install-apt-common.sh`, `install-yq.sh`, `Install-Toolset.ps1`)

You should also leave in the `configure-*.sh` scripts for software that we install.
(Remove `configure-*.sh` for sofware we remove.)

### Miscellaneous Changes

In addition to installing software, the Packer template does a lot of work doing things like generating the [software list .md file][4] and copying Azure-specific config files.
This work is not needed for our purposes, so remove it as well.

You can also remove a step that runs `RunAll-Tests.ps1`.
The installation scripts already run the specific tests for the software they install, and `RunAll-Tests.ps1` runs tests for software that we remove.

## Editing the Toolset JSON

In addition to the Packer HCL files, this repo also contains files defining the actual versions of software to install at `images/ubuntu/toolsets/toolset-<version>.json`.
The February 2024 release edits 4 sections of these files: `"toolcache"`, `"powershellModules"`, `"apt"`, and `"node_modules"`.

- The `"toolcache"` section defines the versions of software preinstalled in `/opt/hostedtoolcache`.
  Remove everything except the "Python", "PyPy", and "node" sections.
- The `"powershellModules"` section defines various Powershell modules to be installed on the runner.
  Only "Pester" is needed (to run tests during the Packer build); everything else can be removed.
- The `"apt"` section defines packages to be installed by Apt.
  Feel free to remove software from this section, but be juicious about it.
  However, be sure to add `"vim-tiny"` to the `"common_packages"` list and `"nano"` to the `"cmd_packages"` list, so the runner images have text editors preinstalled.
- The `"node_modules"` section defines NodeJS modules to be installed on the runner.
  Most of these are only relevant to web development, and can be removed.
  (Most actions run `npm install` or similar to install all their depenencies locally, anyway.)
  Be sure to keep `"n"` and `"yarn"`, however.

For an example, see [this diff][6].

## Editing the Install Scripts

In the scripts that are still run by the Packer template, you'll need to remove a few commands.
An example of this kind of change is in [this commit][5].

- Remove `journalctl` and `systemctl` commands, since `journald` and `systemd` are not used in Docker images.
- Remove commands that edit `/etc/waagent.conf` and `/etc/default/motd-news`, since neither of those files are present in our images.
- Remove commands that edit `/etc/hosts`, since that file is mounted by Docker and cannot be edited (at least by `sed`).

## Building the Images

To build the new images, navigate to `images/ubuntu/templates` run the following commands for each Ubuntu version.

```shellsession
$ # Ensure the template syntax is correct
$ packer validate ubuntu-<version>.pkr.hcl
The configuration is valid.

$ # Ensure the required plugins are installed
$ packer init ubuntu-<version>.pkr.hcl  

$ # Build the template
$ packer build ubuntu-<version>.pkr.hcl
```

Once the images are built, push them to DockerHub:

```sh
docker push bugswarm/githubactionsjobrunners:<year>.<month>.ubuntu-<version>
```

And update the default version of those images:

```sh
docker tag bugswarm/githubactionsjobrunners:<year>.<month>.ubuntu-<version> \
    bugswarm/githubactionsjobrunners:ubuntu-<version>
docker push bugswarm/githubactionsjobrunners:ubuntu-<version>
```

### Debugging

If the build fails, Packer will list which step/script was the one that failed.
In addition, this repo defines several tests that are run during the build to make sure that tools are correctly installed.

[1]: <https://developer.hashicorp.com/packer>
[2]: <https://developer.hashicorp.com/packer/docs/templates/hcl_templates>
[3]: <https://github.com/BugSwarm/virtual-environments/compare/main...BugSwarm:virtual-environments:feb24-docker#diff-3fb175943bb0cb761263144e7dbb5d4955ae2d951beaec7086a27057641b6a15>
[4]: <https://github.com/BugSwarm/virtual-environments/blob/bugswarm/feb2024/images/ubuntu/Ubuntu2204-Readme.md>
[5]: <https://github.com/bugswarm/virtual-environments/commit/ca2d2a859384a2ab239db8af6a3f7d632b52577d>
[6]: <https://github.com/BugSwarm/virtual-environments/compare/main...BugSwarm:virtual-environments:feb24-docker#diff-be8309c2f1b885c28cd700b77c90b395634d4ce945ec801f23999d1f8aebc696>
