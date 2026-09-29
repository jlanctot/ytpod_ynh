#!/bin/bash
# shellcheck disable=SC1091
source /usr/share/yunohost/helpers

app=$YNH_APP_INSTANCE_NAME
final_path=$(ynh_app_setting_get --app=$app --key=path || true)
app_etc_dir="/etc/$app"
app_log_dir="/var/log/$app"
app_log_file="$app_log_dir/$app.log"
