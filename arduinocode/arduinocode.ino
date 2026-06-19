/*
  Sensor Panel â€” Arduino sketch
  Reads 5 touch sensors + an MPU6050 gyroscope and streams readings to the
  laptop as one JSON line over USB serial, about 10 times per second.

  ASSUMPTIONS (tell me if either is wrong and I'll adjust the code):
    - The gyroscope is an MPU6050 / GY-521 module (the common cheap one)
    - The touch sensors are simple digital modules (e.g. TTP223) that
      output HIGH while touched, LOW otherwise

  WIRING
    Touch sensors 1-5  ->  Arduino pins 2, 3, 4, 5, 6  (signal pin)
    Touch sensors      ->  VCC to 5V, GND to GND
    MPU6050 SDA        ->  A4 (Uno/Nano) | pin 20 (Mega) | pin 2 (Leonardo)
    MPU6050 SCL        ->  A5 (Uno/Nano) | pin 21 (Mega) | pin 3 (Leonardo)
    MPU6050            ->  VCC to 5V (check your module â€” some want 3.3V),
                            GND to GND

    Note: on a Leonardo, SDA/SCL share pins with touch sensors 2 & 3 â€”
    move those two touch sensors to different pins if using a Leonardo.

  No extra libraries needed - only the built-in Wire library.
*/

#include <Wire.h>

const int MPU_ADDR = 0x68;  // default MPU6050 I2C address
const int TOUCH_PINS[5] = {2, 3, 4, 5, 6};

void setup() {
  Serial.begin(115200);
  Wire.begin();

  // MPU6050 starts in sleep mode - wake it up by writing 0 to the power
  // management register (0x6B).
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);
  Wire.write(0);
  Wire.endTransmission(true);

  for (int i = 0; i < 5; i++) {
    pinMode(TOUCH_PINS[i], INPUT);
  }
}

void loop() {
  // --- Read gyroscope: registers 0x43-0x48 hold gyro X, Y, Z ---
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x43);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 6, true);

  int16_t rawX = Wire.read() << 8 | Wire.read();
  int16_t rawY = Wire.read() << 8 | Wire.read();
  int16_t rawZ = Wire.read() << 8 | Wire.read();

  // Convert to degrees/second (default sensitivity: 131 LSB per deg/s)
  float gx = rawX / 131.0;
  float gy = rawY / 131.0;
  float gz = rawZ / 131.0;

  // --- Read touch sensors and send everything as one JSON line ---
  Serial.print("{\"touch\":[");
  for (int i = 0; i < 5; i++) {
    Serial.print(digitalRead(TOUCH_PINS[i]));
    if (i < 4) Serial.print(",");
  }
  Serial.print("],\"gyroX\":");
  Serial.print(gx, 2);
  Serial.print(",\"gyroY\":");
  Serial.print(gy, 2);
  Serial.print(",\"gyroZ\":");
  Serial.print(gz, 2);
  Serial.println("}");

  delay(100);  // ~10 readings per second
}