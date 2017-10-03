#! /bin/bash
set -e

# import common functions
source _common.sh

# we only want to run this script for "xx" polarization
pol1="xx"

basename=$(basename $1 uv)

if is_same_pol $basename $pol1; then
    # get metrics filename
    nopol_base=$(remove_pol $basename)
    echo rm -rf ${nopol_base}HH.uv.omni.calfits.omni_metrics.json
    rm -rf $1 ${nopol_base}HH.uv.omni.calfits.omni_metrics.json
fi
