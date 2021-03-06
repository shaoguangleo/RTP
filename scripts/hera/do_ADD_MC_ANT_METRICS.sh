#!/bin/bash
set -e

# import common functions
src_dir="$(dirname "$0")"
source ${src_dir}/_common.sh

# define polarization
pol1="xx"

fn=$(basename ${1} uv)

# only upload from the "main" polarization thread
if is_same_pol $fn $pol1; then
    # get ant_metrics filename
    nopol_base=$(remove_pol $fn)
    metrics_f=`echo ${nopol_base}HH.uv.ant_metrics.json`
    echo add_qm_metrics.py --type=ant ${metrics_f}
    add_qm_metrics.py --type=ant ${metrics_f}
fi
