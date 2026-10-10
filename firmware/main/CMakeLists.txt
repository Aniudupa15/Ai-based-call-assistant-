if(NOT EXISTS "${COMPONENT_DIR}/config.h")
    message(FATAL_ERROR "firmware/main/config.h is missing: copy config.example.h to config.h and fill it in")
endif()

idf_component_register(
    SRCS "app_main.c" "call_fsm.c" "modem.c" "audio_bridge.c" "pc_link.c"
         "voicemail.c" "sync.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_wifi esp_netif esp_event nvs_flash esp_http_client fatfs
             sdmmc json esp_timer)
