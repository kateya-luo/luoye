// Luoye firmware build identity. PROJECT_VER in the root CMakeLists is the
// single version source used by the ESP app descriptor, UI and serial log.
#pragma once

#include <stdbool.h>

const char *luoye_build_product_name(void);
const char *luoye_build_product_name_zh(void);
const char *luoye_build_version(void);
const char *luoye_build_git_commit(void);
const char *luoye_build_hardware_rev(void);
const char *luoye_build_flavor(void);
const char *luoye_build_api_contract(void);
const char *luoye_build_device_auth_profile(void);
const char *luoye_build_server_release(void);
const char *luoye_build_min_client_version(void);
bool luoye_build_git_dirty(void);

// Prints machine-parseable LY|BOOT and LY|HW records. No credentials or user
// content are included.
void luoye_build_log_boot(void);
