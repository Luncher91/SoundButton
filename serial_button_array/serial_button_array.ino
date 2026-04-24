#include <Arduino.h>
#include <Button.h>

const gpio_num_t btn_pins[] {
  // right
  GPIO_NUM_2,
  GPIO_NUM_4,
  GPIO_NUM_16,
  GPIO_NUM_17,
  GPIO_NUM_5,
  GPIO_NUM_18,
//  GPIO_NUM_19,
//  GPIO_NUM_21,
//  GPIO_NUM_22,
//  GPIO_NUM_23,
  // left
  GPIO_NUM_13,
  GPIO_NUM_14,
  GPIO_NUM_27,
  GPIO_NUM_26,
  GPIO_NUM_25,
  GPIO_NUM_33
};

Button * btn[18];
int btnCmd[18];

void setup() {
  // put your setup code here, to run once:
  Serial.begin(115200);

  size_t array_length = sizeof(btn_pins) / sizeof(btn_pins[0]);
  for(int i = 0; i < array_length; i++){
    btn[i] = new Button(btn_pins[i], true);
    btnCmd[i] = i + 1;
    btn[i]->attachPressDownEventCb(&btnPd, &btnCmd[i]);
  }
  
  Serial.println("");
  Serial.println("Hello from ButtonArray");
}

static void btnPd(void *button_handle, void *usr_data) {
  Serial.print("BTN");
  int nr = (int)(*(int*)usr_data);
  Serial.println(nr);
}

void loop() {
  delay(1000);
  Serial.println("HEART_BEAT");
}
