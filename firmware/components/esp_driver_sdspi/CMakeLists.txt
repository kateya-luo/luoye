idf_build_get_property(target IDF_TARGET)

if(NOT ${target} STREQUAL "esp32s3")
    message(FATAL_ERROR
        "Project-local esp_driver_sdspi is audited only for ESP32-S3")
endif()

if(NOT IDF_VERSION_MAJOR EQUAL 5 OR
   NOT IDF_VERSION_MINOR EQUAL 5 OR
   NOT IDF_VERSION_PATCH EQUAL 4)
    message(FATAL_ERROR
        "Project-local esp_driver_sdspi must be re-audited outside ESP-IDF 5.5.4")
endif()

set(srcs)

set(public_include "include")

if(CONFIG_SOC_GPSPI_SUPPORTED)
    list(APPEND srcs "src/sdspi_crc.c"
                     "src/sdspi_host.c"
                     "src/sdspi_transaction.c")
endif()

if(${target} STREQUAL "linux")
    set(requires "")
    set(priv_requires esp_timer)
else()
    set(requires sdmmc esp_driver_spi esp_driver_gpio)
    set(priv_requires esp_timer esp_mm)
endif()

idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS ${public_include}
                       REQUIRES "${requires}"
                       PRIV_REQUIRES "${priv_requires}"
                      )
