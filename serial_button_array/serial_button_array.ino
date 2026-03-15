#include <Arduino.h>
#include <Button.h>

#define BTN1_PIN GPIO_NUM_13

Button * btn;

void setup() {
  // put your setup code here, to run once:
  Serial.begin(115200);

  btn = new Button(BTN1_PIN, true);
  btn->attachPressDownEventCb(&onButtonPressDownCb, NULL);
  
  Serial.println("");
  Serial.println("Hello from ButtonArray");
}

static void onButtonPressDownCb(void *button_handle, void *usr_data) {
  Serial.println("BTN1");
}

void loop() {
  delay(1000);
  Serial.println("HEART_BEAT");
}
