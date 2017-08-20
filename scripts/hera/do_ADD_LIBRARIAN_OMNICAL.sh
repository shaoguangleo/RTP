#! /bin/bash
set -e

# import common functions
source _common.sh

conn=${1}
store_path=${2}
basename=${3}

# define polarization
pol1="xx"

fn=$(basename ${basename} uv)

# only run this for one polarization type
if is_same_pol $fn $pol1; then
    nopol_base=$(remove_pol $fn)
    omni_f=`echo ${nopol_base}HH.uv.omni.calfits`
    total_path=`echo ${store_path}/${omni_f}`
    echo upload_to_librarian.py ${conn} ${omni_f} ${total_path}
    upload_to_librarian.py ${conn} ${omni_f} ${total_path}
fi
