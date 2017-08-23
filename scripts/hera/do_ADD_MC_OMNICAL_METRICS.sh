#!/bin/bash
set -e

# load common functions
source _common.sh

# define polarization
pol1="xx"

fn=$(basename ${1} uv)

# only upload from the "main" polarization thread
if is_same_pol $fn $pol1; then
    # get omnical solutions filename
    nopol_base=$(remove_pol $fn)
    omni_f=`echo ${nopol_base}HH.uv.omni.calfits`
    echo add_qm_metrics.py --type=omnical ${omni_f}
    add_qm_metrics.py --type=omnical ${omni_f}
fi