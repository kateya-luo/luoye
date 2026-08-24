// board_pins.h — AI 录音卡引脚定义
// 来源:1Netlist_AI_RECORD_2026-07-10.tel(嘉立创EDA网表)+ BOM 交叉验证,2026-07-10
// 校验锚点:USB D-/D+ 在 GPIO19/20(硅片固定)、UART0 在 GPIO43/44、
//          模块 28/29/30 脚(IO35/36/37)悬空(N16R8 八线 PSRAM 占用)——三点全部吻合。
#pragma once

// ---------- 按键(无外部上拉,仅 100nF 去抖电容 → 必须开内部上拉;按下拉低) ----------
#define PIN_KEY_REC        4    // SW3, RTC-IO,可作深睡唤醒源
#define PIN_KEY_MARK       2    // SW4, RTC-IO
#define PIN_KEY_BACK       1    // SW5, RTC-IO
#define PIN_KEY_BOOT       0    // SW7, strap 脚(下载模式),运行期可作普通输入

// ---------- LED(高电平点亮,串 2.2k) ----------
#define PIN_LED_REC        48   // LED1  红:录音常亮/暂停慢闪/标记双闪/收尾快闪
#define PIN_LED_FULL       45   // LED3  黄:SD将满常亮/离线或积压≥30s慢闪/提醒闪
                                //       ⚠ strap 脚(VDD_SPI 电压),上电瞬间勿被外部强拉
#define PIN_LED_CHG        16   // LED_CHG 绿:充电中常亮

// ---------- 墨水屏 GDEY0213B74 (SSD1680, 250×122, SPI2) ----------
#define PIN_EPD_SCLK       3    // ⚠ strap 脚(JTAG源),做 SPI CLK 无碍
#define PIN_EPD_MOSI       46   // ⚠ strap 脚:启动瞬间必须为低,屏端不得有上拉
#define PIN_EPD_CS         10
#define PIN_EPD_DC         18
#define PIN_EPD_RST        21   // ← 见文末「板改建议」:可与 RTC_INT 互换
#define PIN_EPD_BUSY       14   // 输入,高=忙
#define PIN_EPD_POWER_EN   47   // TPS22918 负载开关,高=给屏供电(R24 100k 默认下拉关断)

// ---------- microSD (SPI3, 全部 22Ω 串阻) ----------
#define PIN_SD_CS          17
#define PIN_SD_MOSI        11
#define PIN_SD_SCK         12
#define PIN_SD_MISO        13
// SD 供电经 R_SD_PWR(0Ω)常供;如日后改 MOS 控制可做掉电省电

// ---------- 双 PDM 麦克风 IM72D128(副板,一根数据线立体声) ----------
#define PIN_PDM_CLK        5    // 22Ω 串阻
#define PIN_PDM_DATA       6    // 22Ω 串阻;U1 SELECT=GND(L),U2 SELECT=VDD(R)
#define PIN_MIC_LDO_EN     7    // TPS7A2033 EN,高=开麦供电;不录音时拉低省电

// ---------- I2C 总线(BQ25186 充电 0x6A / MAX17048 电量计 0x36 / PCF8563 RTC 0x51) ----------
#define PIN_I2C_SDA        8    // MAX17048/PCF8563 正常方向；BQ25186 样板焊盘接反
#define PIN_I2C_SCL        9    // MAX17048/PCF8563 正常方向；BQ25186 样板焊盘接反
#define I2C_ADDR_BQ25186   0x6A
#define I2C_ADDR_MAX17048  0x36
#define I2C_ADDR_PCF8563   0x51

// ---------- 电源管理信号 ----------
#define PIN_PWR_MODE       15   // TPS63001 PS/SYNC:高=强制PWM(录音低噪),低=PFM省电
#define PIN_BQ_CE_N        38   // 充电使能,低有效;板上 R16 10k 下拉 → 默认允许充电,可悬空
#define PIN_BQ_INT_N       39   // 充电中断,低有效(⚠ 非 RTC-IO,不能深睡唤醒)
#define PIN_BQ_PG_N        40   // Power-Good,低=USB在位(⚠ 非 RTC-IO)
#define BQ25186_BOOT_CHARGE_CURRENT_MA 1000 // 1000mAh 电芯：上电即请求 1A CC
#define BQ25186_BOOT_INPUT_LIMIT_MA  1050 // 输入限流包含系统负载，不能等同于电池充电电流
#define BQ25186_LOW_SOC_CHARGE_MA    1000 // BQ25186 自行从 CC 进入 CV 并逐步减流
#define BQ25186_MID_SOC_CHARGE_MA    1000 // 不再按电量计 SOC 提前降低 ICHG
#define BQ25186_HIGH_SOC_CHARGE_MA   1000 // 不以 MAX17048 的 100% 代替充电终止
#define BQ25186_LOW_SOC_INPUT_MA     1050 // 系统负载会由 ILIM/DPPM 自动从电池充电电流中让路
#define BQ25186_MID_SOC_INPUT_MA     1050
#define BQ25186_HIGH_SOC_INPUT_MA    1050
#define PIN_BAT_ALRT       42   // MAX17048 低电报警,低有效(⚠ 非 RTC-IO)
#define PIN_RTC_INT        41   // PCF8563 闹钟中断,低有效
                                // ⚠⚠ GPIO41 非 RTC-IO:深睡时闹钟无法唤醒芯片!
                                //     当前方案:有待办提醒时用 light sleep;详见 README「板改建议」

// ---------- USB / 调试串口(固定,不可改) ----------
#define PIN_USB_DN         19
#define PIN_USB_DP         20
#define PIN_UART0_TX       43
#define PIN_UART0_RX       44

// ---------- SPI 主机分配 ----------
#define EPD_SPI_HOST       SPI2_HOST
#define SD_SPI_HOST        SPI3_HOST
