#include <WiFiS3.h>

const int IR_PIN = 4;

// เปลี่ยนเป็น false หาก IR sensor ของคุณส่งค่า LOW เมื่อมีวัตถุ
const bool IR_ACTIVE_HIGH = true;

// กรอกค่าเฉพาะเครื่องก่อน Upload และอย่า commit ค่าจริงลง GitHub
const char WIFI_SSID[] = "YOUR_WIFI_NAME";
const char WIFI_PASSWORD[] = "YOUR_WIFI_PASSWORD";
const char API_HOST[] = "iot.example.com";
const int API_PORT = 443;
const char API_PATH[] = "/api/arduino/ir";
const char API_KEY[] = "YOUR_ARDUINO_API_KEY";

WiFiSSLClient client;

void connectWiFi() {
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print("กำลังเชื่อมต่อ Wi-Fi: ");
    Serial.println(WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    const unsigned long startedAt = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - startedAt < 10000) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
  }

  Serial.print("Wi-Fi connected, IP: ");
  Serial.println(WiFi.localIP());
}

bool sendIRStatus(bool objectPresent) {
  const String payload = String("{\"object_present\":")
    + (objectPresent ? "true" : "false") + "}";

  if (!client.connect(API_HOST, API_PORT)) {
    Serial.println("เชื่อมต่อ Dashboard API ไม่สำเร็จ");
    return false;
  }

  client.print("POST ");
  client.print(API_PATH);
  client.println(" HTTP/1.1");
  client.print("Host: ");
  client.println(API_HOST);
  client.print("X-API-Key: ");
  client.println(API_KEY);
  client.println("Content-Type: application/json");
  client.print("Content-Length: ");
  client.println(payload.length());
  client.println("Connection: close");
  client.println();
  client.println(payload);

  const unsigned long startedAt = millis();
  while (client.connected() && millis() - startedAt < 1500) {
    while (client.available()) {
      client.read();
    }
  }
  client.stop();
  return true;
}

void setup() {
  Serial.begin(115200);
  pinMode(IR_PIN, INPUT);
  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  const int sensorValue = digitalRead(IR_PIN);
  const bool objectPresent = IR_ACTIVE_HIGH
    ? sensorValue == HIGH
    : sensorValue == LOW;

  Serial.print("IR object_present: ");
  Serial.println(objectPresent ? "true" : "false");
  sendIRStatus(objectPresent);

  delay(1000);
}
