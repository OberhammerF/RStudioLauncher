#!/bin/sh
#SBATCH --time=08:00:00      # Adjust to change max uptime of RStudio Server
#SBATCH --signal=USR2
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1    # Adjust to fit CPU needs
#SBATCH --gres=tmpspace:20G
#SBATCH --mem=32G            # Adjust to fit memory needs
#SBATCH --output=/home/pmc_research/%u/rstudio-server.job.%j
# customize --output path as appropriate (to a directory readable only by the user!)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  RStudio Server on HPC — SLURM batch script                               ║
# ║                                                                            ║
# ║  Run with:  sbatch /path/to/rstudio_server.sh                             ║
# ║                                                                            ║
# ║  This script launches an RStudio Server Apptainer image on a compute node. ║
# ║  Connection instructions are written to ~/rstudio-server.job.<jobid>       ║
# ║                                                                            ║
# ║  Adapted from the Rocker project (rocker-project.org/use/singularity)      ║
# ║  Damon Hofman (2022), Amalia Nabuurs (2023), Franziska Oberhammer (2024)   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


# ┌────────────────────────────────────────────────────────────────────────────┐
# │  *** USER CONFIGURATION — CHANGE THESE ***                                │
# └────────────────────────────────────────────────────────────────────────────┘

# R version — must match a .sif image in singularity_dir (see below)
# Example: "4.3.1", "4.3.2", "4.4.0"
version="CHANGE_ME"

# Path to the directory containing Apptainer .sif images
# If your group already has images set up, use that shared path.
singularity_dir="/hpc/pmc_kuiper/singularity"

# Where RStudio stores session data (bookmarks, history, etc.)
# Create this directory first!  e.g.: mkdir -p /hpc/pmc_kuiper/yourUsername/rStudioSessions
XDG_DATA_HOME_PATH="CHANGE_ME"

# Where R packages for this version are installed (shared across users in a group)
# e.g.: /hpc/pmc_kuiper/Rstudio_Server_Libs/Rstudio_4.3.1_libs
R_LIBS_USER_PATH="/hpc/pmc_kuiper/Rstudio_Server_Libs/Rstudio_${version}_libs"

# Path to the Python .sif image (used to find a free port)
PYTHON_SIF="${singularity_dir}/python_3.11.3.sif"


# ┌────────────────────────────────────────────────────────────────────────────┐
# │  *** SCRIPT LOGIC — normally no changes needed below this line ***        │
# └────────────────────────────────────────────────────────────────────────────┘

workdir=${TMPDIR}

mkdir -p -m 700 ${workdir}/run ${workdir}/tmp ${workdir}/var/lib/rstudio-server
cat > ${workdir}/database.conf <<END
provider=sqlite
directory=/var/lib/rstudio-server
END

# Set OMP_NUM_THREADS to prevent OpenBLAS (and any other OpenMP-enhanced
# libraries used by R) from spawning more threads than the number of processors
# allocated to the job.
#
# Set R_LIBS_USER to a path specific to rocker/rstudio to avoid conflicts with
# personal libraries from any R installation in the host environment

cat > ${workdir}/rsession.sh <<END
#!/bin/sh
export OMP_NUM_THREADS=${SLURM_JOB_CPUS_PER_NODE}
export R_LIBS_USER=${R_LIBS_USER_PATH}
export XDG_DATA_HOME=${XDG_DATA_HOME_PATH}
exec /usr/lib/rstudio-server/bin/rsession "\${@}"
END

chmod +x ${workdir}/rsession.sh

export APPTAINER_BIND="${workdir}/run:/run,${workdir}/tmp:/tmp,${workdir}/database.conf:/etc/rstudio/database.conf,${workdir}/rsession.sh:/etc/rstudio/rsession.sh,${workdir}/var/lib/rstudio-server:/var/lib/rstudio-server,/hpc/pmc_kuiper,${R_LIBS_USER_PATH}:/usr/local/lib/R/site-library"

# Do not suspend idle sessions.
export APPTAINERENV_RSTUDIO_SESSION_TIMEOUT=0

export APPTAINERENV_USER=$(id -un)
export APPTAINERENV_PASSWORD=$(openssl rand -base64 15)

# Get unused socket per https://unix.stackexchange.com/a/132524
python="apptainer exec -B /hpc:/hpc ${PYTHON_SIF} python"
readonly PORT=$(${python} -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')

cat 1>&2 <<END
1. SSH tunnel from your workstation using the following command:

   > ssh -N -L 8787:${HOSTNAME}:${PORT} -J ${APPTAINERENV_USER}@hpcgw.op.umcutrecht.nl -l ${APPTAINERENV_USER} hpcs06.op.umcutrecht.nl nc %h %p 2>/dev/null

   OR, if the config file has already been set with e.g. 'ssh gw2hpcs06'

   > ssh -L 8787:${HOSTNAME}:${PORT} gw2hpcs06

   and point your web browser to http://localhost:8787

2. log in to RStudio Server using the following credentials:

   user: ${APPTAINERENV_USER}
   password: ${APPTAINERENV_PASSWORD}

When done using RStudio Server, terminate the job by:

1. Exit the RStudio Session ("power" button in the top right corner of the RStudio window)
2. Issue the following command on the login node:

      scancel -f ${SLURM_JOB_ID}
END

singularity exec --cleanenv ${singularity_dir}/rstudio_${version}_bioconductor.sif \
    rserver --www-port ${PORT} \
            --server-user ${APPTAINERENV_USER} \
            --auth-none=0 \
            --auth-pam-helper-path=pam-helper \
            --auth-stay-signed-in-days=30 \
            --auth-timeout-minutes=0 \
            --rsession-path=/etc/rstudio/rsession.sh
printf 'rserver exited' 1>&2
