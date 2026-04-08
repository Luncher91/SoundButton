#include <Arduino.h>
#include <Button.h>

#define BTN01_PIN GPIO_NUM_02
#define BTN02_PIN GPIO_NUM_03
#define BTN03_PIN GPIO_NUM_04
#define BTN04_PIN GPIO_NUM_05
#define BTN05_PIN GPIO_NUM_13
#define BTN06_PIN GPIO_NUM_15
#define BTN07_PIN GPIO_NUM_16
#define BTN08_PIN GPIO_NUM_17
#define BTN09_PIN GPIO_NUM_18
#define BTN10_PIN GPIO_NUM_19
#define BTN11_PIN GPIO_NUM_21
#define BTN12_PIN GPIO_NUM_22
#define BTN13_PIN GPIO_NUM_23
#define BTN14_PIN GPIO_NUM_25
#define BTN15_PIN GPIO_NUM_26
#define BTN16_PIN GPIO_NUM_27
#define BTN17_PIN GPIO_NUM_32
#define BTN18_PIN GPIO_NUM_33

Button * btn1;

void setup() {
  // put your setup code here, to run once:
  Serial.begin(115200);

  btn1 = new Button(BTN01_PIN, true);
  btn1->attachPressDownEventCb(&btn1Pd, NULL);
  
  Serial.println("");
  Serial.println("Hello from ButtonArray");
}

static void btn1Pd(void *button_handle, void *usr_data) {
  Serial.println("BTN1");
}

void loop() {
  delay(1000);
  Serial.println("HEART_BEAT");
}
