cmake_minimum_required(VERSION 3.16)

# Luoye firmware release identity. PROJECT_VER is the only firmware-version
# source; the UI and startup log read the ESP app descriptor generated from it.
set(PROJECT_VER "2.0.0")
set(LUOYE_PRODUCT_NAME "Luoye" CACHE STRING "Internal product name")
set(LUOYE_PRODUCT_NAME_ZH "落叶" CACHE STRING "Internal product name (Chinese)")
set(LUOYE_HARDWARE_REV "LY-HW-ENG-20260710" CACHE STRING "Target PCB revision")
set(LUOYE_BUILD_FLAVOR "engineering" CACHE STRING "dev, rc, release, or engineering")
set(LUOYE_API_CONTRACT "luoye-device-api/2" CACHE STRING "Compatible device API contract")
set(LUOYE_DEVICE_AUTH_PROFILE "engineering" CACHE STRING
    "Required server device authentication profile")
# Protocol compatibility is release identity, not a reusable build-cache knob.
# Keep these normal variables so an older build directory cannot silently
# stamp stale server/client versions into a new firmware package.
set(LUOYE_SERVER_RELEASE "2.0.0")
set(LUOYE_MIN_CLIENT_VERSION "2.0.0")
set(LUOYE_SERVER_BASE_URL "https://meeting.example.invalid" CACHE STRING
    "ClearMeeting API origin without a trailing slash")
set(LUOYE_LAN_WIFI_SSID "" CACHE STRING
    "Optional Wi-Fi SSID that selects a private ClearMeeting endpoint")
set(LUOYE_LAN_SERVER_BASE_URL "" CACHE STRING
    "Optional private ClearMeeting origin used only on LUOYE_LAN_WIFI_SSID")
option(LUOYE_ALLOW_INSECURE_HTTP
       "Allow plaintext HTTP for dev-profile LAN integration only" OFF)

if(NOT LUOYE_DEVICE_AUTH_PROFILE STREQUAL "engineering")
  message(FATAL_ERROR
    "Luoye 2.0.0 is frozen to device_auth_profile=engineering")
endif()
if((LUOYE_LAN_WIFI_SSID STREQUAL "" AND
    NOT LUOYE_LAN_SERVER_BASE_URL STREQUAL "") OR
   (NOT LUOYE_LAN_WIFI_SSID STREQUAL "" AND
    LUOYE_LAN_SERVER_BASE_URL STREQUAL ""))
  message(FATAL_ERROR
    "LUOYE_LAN_WIFI_SSID and LUOYE_LAN_SERVER_BASE_URL must be set together")
endif()
if(NOT LUOYE_LAN_SERVER_BASE_URL STREQUAL "" AND
   NOT LUOYE_LAN_SERVER_BASE_URL MATCHES "^https?://[A-Za-z0-9.-]+(:[0-9]+)?$")
  message(FATAL_ERROR
    "LUOYE_LAN_SERVER_BASE_URL must be an http(s) origin without a path")
endif()

if(NOT LUOYE_SERVER_BASE_URL MATCHES "^https?://[A-Za-z0-9.-]+(:[0-9]+)?$")
  message(FATAL_ERROR
    "LUOYE_SERVER_BASE_URL must be an http(s) origin without a path or trailing slash")
endif()
if(LUOYE_SERVER_BASE_URL MATCHES "^http://")
  if(NOT LUOYE_ALLOW_INSECURE_HTTP)
    message(FATAL_ERROR
      "Plain HTTP requires -DLUOYE_ALLOW_INSECURE_HTTP=ON")
  endif()
  if(NOT LUOYE_BUILD_FLAVOR STREQUAL "dev" AND
     NOT LUOYE_BUILD_FLAVOR STREQUAL "engineering")
    message(FATAL_ERROR
      "Plain HTTP is restricted to dev/engineering builds")
  endif()
endif()

find_package(Git QUIET)
set(LUOYE_GIT_COMMIT "unknown")
set(LUOYE_GIT_DIRTY 1)
if(Git_FOUND)
  execute_process(
    COMMAND "${GIT_EXECUTABLE}" rev-parse --short=12 HEAD
    WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}"
    OUTPUT_VARIABLE LUOYE_GIT_COMMIT
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE LUOYE_GIT_RESULT
    ERROR_QUIET
  )
  if(NOT LUOYE_GIT_RESULT EQUAL 0 OR LUOYE_GIT_COMMIT STREQUAL "")
    set(LUOYE_GIT_COMMIT "unknown")
  endif()

  execute_process(
    COMMAND "${GIT_EXECUTABLE}" status --porcelain
    WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}"
    OUTPUT_VARIABLE LUOYE_GIT_STATUS
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
  )
  if(LUOYE_GIT_STATUS STREQUAL "")
    set(LUOYE_GIT_DIRTY 0)
  endif()
endif()

# This product branch targets the 1.54-inch GDEY0154D67 exclusively.  The
# legacy 2.13-inch component exports the same public epd_* symbols; if both
# libraries are present, archive link order can silently select SSD1680 and
# send a 250x122 frame to the 200x200 SSD1681 panel (visible as full-screen
# noise/stripes).  Exclude it at component discovery, not merely from REQUIRES.
set(EXCLUDE_COMPONENTS epd_ssd1680)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(recorder_card)
