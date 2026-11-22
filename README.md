# IoT Vision Dashboard

ระบบตรวจจับอุปกรณ์ด้วยกล้องและ YOLO โดยใช้ IR Sensor จาก Arduino UNO R4 WiFi เป็นตัวเริ่มรอบการจับภาพ

## Flow การทำงาน

1. Arduino ส่ง `object_present` ผ่าน HTTPS ไปยัง Dashboard
2. Dashboard สร้างคำขอจับภาพหนึ่งรอบ
3. หน้าเว็บเปิดกล้องของเบราว์เซอร์และส่งภาพให้ API วิเคราะห์
4. ระบบบันทึกภาพ เวลา และชื่ออุปกรณ์ลง PostgreSQL
5. หน้าเว็บปิดกล้องอัตโนมัติหลังบันทึกสำเร็จ
6. เมื่อ Arduino ส่ง `false` ต่อเนื่องครบ 10 วินาที ระบบจึงพร้อมรับรอบใหม่

สถานะกันการบันทึกซ้ำถูกเก็บใน PostgreSQL จึงไม่เริ่มรอบใหม่เพียงเพราะ container restart

## โครงสร้าง AWS

ใช้ AWS Project ชื่อ `Future Startup` ใน region `ap-southeast-2` โดยรัน Docker บน EC2 และใช้ PostgreSQL ใน Docker volume เดียวกับเครื่อง:

- EC2 Linux ขนาดที่อยู่ใน Free Tier/เครดิตของบัญชี เช่น `t3.small` เพื่อให้ YOLO มีหน่วยความจำพอ
- Docker Compose: Dashboard, PostgreSQL และ Caddy HTTPS
- PostgreSQL และรูปภาพเก็บบน EBS volume ของ EC2
- ใช้ hostname แบบ `nip.io` จาก Elastic IP ได้ จึงไม่ต้องซื้อโดเมนเพิ่ม
- GitHub Actions ตรวจสอบโค้ดและ deploy ทุกครั้งที่ push เข้า `main`

AWS แสดงเครดิตคงเหลือและวันหมดอายุในหน้า Console Home ของบัญชีนี้แล้ว ควรตรวจสอบ Billing เป็นระยะ เพราะการใช้ฟรีขึ้นกับ Free Tier/เครดิตและชนิด instance ที่เลือก

## รันในเครื่องด้วย Docker

```powershell
docker compose up --build
```

เปิด `http://localhost:8000` แล้วอนุญาตสิทธิ์กล้อง รูป local อยู่ที่ `data/snapshots` และข้อมูลอยู่ใน Docker volume

## ตั้งค่า Arduino

แก้ค่าก่อน Upload ในไฟล์ `firmware/Project_IoT.ino`:

```cpp
const char WIFI_SSID[] = "ชื่อ Wi-Fi";
const char WIFI_PASSWORD[] = "รหัสผ่าน Wi-Fi";
const char API_HOST[] = "โดเมน HTTPS ของ Dashboard";
const char API_KEY[] = "ค่าเดียวกับ ARDUINO_API_KEY";
```

ใช้ `WiFiSSLClient` และพอร์ต `443` ห้ามใช้ `localhost`, `127.0.0.1` หรือ IP ภายในเครื่อง เพราะ Arduino ต้องเข้าถึง VM ผ่านอินเทอร์เน็ต

อย่า commit ค่า Wi-Fi หรือ API key จริงลง GitHub

## รับรายการอุปกรณ์ผ่าน MQTT

Dashboard เชื่อมต่อ broker `broker.hivemq.com:8884/mqtt` ผ่าน WebSocket + TLS โดยใช้ prefix `armbot-010eb21b` และ subscribe
`armbot-010eb21b/cmd/queue` เพื่อรับรายการที่กำลังจะหยิบ จากนั้น publish event ไปที่
`armbot-010eb21b/event` และ publish retained status ไปที่ `armbot-010eb21b/inspection/status` สามารถกำหนด
`MQTT_HOST`, `MQTT_PORT`, `MQTT_QUEUE_TOPIC`, `MQTT_EVENT_TOPIC`,
`MQTT_STATUS_TOPIC`

Dashboard รับสถานะคิวจาก `armbot-010eb21b/status` เมื่อ ARMbot ยืนยันว่า
คิวเดียวกันถูกยกเลิก Dashboard จะหยุดรอ ปิดกล้องถ้ากำลังตรวจ และเก็บคิวไว้ในประวัติว่า
“ยกเลิกแล้ว” รองรับทั้ง `cancelled` สำหรับคิวที่ยังรอ และ `failed` พร้อมข้อความ
`stopped` สำหรับคิวที่ถูกหยุดระหว่างหยิบ กล้องเปิดทันทีเมื่อ IR ตรวจพบของตามรอบปกติ

เมื่อ ARMbot รายงาน `jobs[].state: "done"` ของคิวที่ตรงกัน Dashboard จะให้เวลา IR
อีก 10 วินาที หากยังไม่มีสัญญาณพบของ จะเปิดกล้องในเบราว์เซอร์และตรวจสำรอง
ประมาณ 20 วินาที นับจากเริ่มประมวลผลภาพ หากพบครบจะบันทึกรูปและแจ้ง `completed`
หากหมดเวลาจะปิดกล้องและบันทึกผล `not_found` หรือ `incomplete`
เปิด Dashboard ค้างไว้และอนุญาตกล้อง สำหรับคิวที่มี Unknown ทั้งการเปิดจาก IR
และการเปิดสำรองจะถ่ายภาพแรกแล้วส่งให้ทีมตรวจ Unknown

ปุ่ม **ตรวจอุปกรณ์** อยู่ข้างทุกคิวในประวัติ กดเพื่อใช้กล้องตรวจรายการของคิวนั้นได้ทันที
คิวปัจจุบันที่ยังรอจะเริ่มตรวจโดยใช้คิวเดิม ส่วนคิวที่จบแล้วจะสร้างรายการตรวจซ้ำ
ที่เชื่อมกับคิวเดิมและเก็บผลเดิมไว้ คิวที่ใช้โมเดลใน Dashboard มีเวลาตรวจด้วยตนเอง
ประมาณ 20 วินาทีนับจากเริ่มประมวลผลภาพ หากยังตรวจไม่ครบก็จะบันทึกผลตามที่พบ
ผลตรวจซ้ำของอุปกรณ์ที่รู้จักเก็บในประวัติ ส่วนคิวที่มี Unknown จะส่งภาพให้ทีมตรวจผ่าน topic เดิม
ผ่าน environment variables

ตัวอย่างส่งคำขอ:

```json
{
  "item_id": "ultrasonic",
  "count": 2,
  "source": "armbot"
}
```

รับ `item_id` หรือ `text` โดยรองรับ `relay`, `triple_syringe`, `ultrasonic` และ `unknown`;
จำนวนรับได้จาก `count` หรือ `quantity` (ค่าเริ่มต้น 1, สูงสุด 100) และ `source`
เป็น optional ระบบสร้าง `job_id` ให้หากไม่ส่งมา เมื่อรับคิวจะส่ง event `queued`
และ status `busy`; หลังรับคิวระบบจะอยู่สถานะ `waiting_ir` และยังไม่เปิดกล้อง
จนกว่า Arduino จะส่ง `object_present: true` เมื่อ IR ตรวจพบของ ระบบจะเปิดกล้อง
ของ Dashboard นี้เพื่อตรวจนับตามรายการในคิว อัปเดตจำนวนคงเหลือ และเมื่อครบจะส่ง
event `completed` พร้อม status `ready` รอบที่เริ่มจาก IR จะรออุปกรณ์ให้ครบโดยไม่มี timeout
เมื่อมีคิวใหม่ ระบบจะเปลี่ยนไปตรวจตามคิวล่าสุดและเก็บคิวเดิมในประวัติเป็น `superseded`
ถ้า IR ยังตรวจพบอุปกรณ์อยู่ ระบบจะเริ่มตรวจคิวล่าสุดด้วยภาพจากรอบกล้องใหม่
ถ้าแท่นว่าง ระบบจะรอ IR ตรวจพบอุปกรณ์ก่อนเปิดกล้อง
คำขอที่ส่ง `job_id` เดิมซ้ำจะใช้คิวเดิมในประวัติ

ลำดับการทำงานคือ เพื่อนกดหยิบ → ARMbot วางอุปกรณ์ → IR ตรวจพบ → Dashboard นี้
เปิดกล้องและตรวจสอบรายการ → แสดงผลว่าครบ/ไม่ครบ → บันทึกรูปผลตรวจและปิดกล้อง
โดยไม่ต้องแก้ Dashboard ของเพื่อน เปิดหน้า Dashboard ค้างไว้ อนุญาตการใช้กล้อง
และเลือกกล้องให้พร้อม ระบบจะแสดงเฉพาะอุปกรณ์ในคิวที่จะหยิบ ไม่ดึง catalog ทั้งหมดมาแสดง
เมื่อ IR ตรวจพบอุปกรณ์ ระบบจะเปิดกล้องและตรวจตามรายการในคิวโดยอัตโนมัติ
ระบบเก็บจำนวนสูงสุดที่เห็นพร้อมกันในแต่ละคลาสระหว่างงาน จึงไม่บวกวัตถุชิ้นเดิมซ้ำจากทุกเฟรม
เมื่อครบจะบันทึกรูปและปิดกล้องตาม flow อัตโนมัติ รูปแบบคำขอ `job_id`/`items` แบบเดิมยังรองรับอยู่ด้วย

กำหนด `MQTT_USERNAME` และ `MQTT_PASSWORD` เป็น environment variables ในเครื่อง
หรือเป็น GitHub Actions secrets ชื่อเดียวกันสำหรับ deploy; ไม่ใส่ credential
ลงใน source code โดยค่าเริ่มต้นใช้ WebSocket + TLS ที่พอร์ต 8884 และ path `/mqtt`

### รับผลตรวจและรูปจาก Dashboard

ให้ฝั่ง ARMbot subscribe **`armbot-010eb21b/inspection/result`** ด้วย QoS 1
บน broker เดียวกับที่ใช้ส่งคิว หรือตั้ง `MQTT_RESULT_TOPIC` เพื่อใช้ชื่อ topic อื่น
Dashboard จะส่ง JSON หนึ่งข้อความเมื่อผลตรวจและรูปยืนยันถูกบันทึกสำเร็จ
และจะลองส่งใหม่หาก broker ยังไม่ยืนยัน การยกเลิกคิวและการเปิดกล้องทดสอบ
จะไม่สร้างข้อความผลตรวจสำเร็จ

ส่ง `job_id` ที่ไม่ซ้ำกันมากับคิว `cmd/queue` เพื่อให้ฝั่งรับเชื่อมผลกับคิวเดิมได้ เช่น

```json
{"job_id":"armbot-2026-0007","item_id":"relay","count":1,"source":"armbot"}
```

ตัวอย่างข้อความตอบกลับ:

```json
{
  "schema_version": 1,
  "type": "inspection_result",
  "status": "completed",
  "job_id": "armbot-2026-0007",
  "armbot_job_id": null,
  "event_id": 57,
  "completed_at": "2026-11-22T10:14:00+00:00",
  "requested": {"Relay module": 1},
  "detected": {"Relay module": 1},
  "image": {
    "content_type": "image/jpeg",
    "encoding": "base64",
    "data": "<JPEG encoded as Base64>"
  }
}
```

- `job_id` ตรงกับรหัสที่ส่งมา หรือเป็นรหัสที่ Dashboard สร้างให้เมื่อคิวไม่มีรหัส
  `armbot_job_id` เป็นสตริงเมื่อทราบรหัสงานของ ARMbot และเป็น `null` เมื่อไม่ทราบ
  หากมีเลขงานนี้ตอนส่งคิว ให้ส่ง `armbot_job_id` เป็นจำนวนเต็มในคำขอได้
- แปลง `image.data` จาก Base64 เป็นไฟล์ JPEG ได้ทันที เช่น `Buffer.from(result.image.data, "base64")` ใน Node.js
- MQTT ใช้ QoS 1 และ `retain=false` ให้ subscribe ก่อนเริ่มสั่งงาน และใช้
  `event_id` เพื่อตัดข้อความซ้ำเมื่อได้ผลตรวจเดิมมากกว่าหนึ่งครั้ง
  การยืนยันจาก broker หมายถึง broker รับข้อความแล้ว
- Broker เริ่มต้นเป็นสาธารณะ ให้ใช้ภาพทดสอบที่แชร์ได้ หรือใช้ broker ส่วนตัว
  ที่จำกัดสิทธิ์ publish/subscribe สำหรับรูปใช้งานจริง

### ผลตรวจสำรองเมื่อหมดเวลา

ฝั่ง ARMbot ใช้ topic ผลตรวจเดิม `armbot-010eb21b/inspection/result` และอ่านผล
`type: "inspection_result"` ที่มี `status: "not_found"` เมื่อไม่พบอุปกรณ์ที่ขอ
หรือ `status: "incomplete"` เมื่อพบเพียงบางส่วน ฝั่งรับควรรองรับสถานะเหล่านี้
ก่อนอ่านรูปและใช้ `result_id` ตัดข้อความ MQTT ซ้ำ

```json
{
  "schema_version": 1,
  "type": "inspection_result",
  "status": "not_found",
  "reason": "camera_timeout",
  "result_id": "queue:42",
  "job_id": "pick-100",
  "armbot_job_id": "18",
  "event_id": null,
  "completed_at": "2026-11-22T10:00:30+00:00",
  "requested": {"Relay module": 1},
  "detected": {"Relay module": 0},
  "image": null
}
```

### ข้อความสำหรับทีมตรวจ Unknown

ให้ subscribe `armbot-010eb21b/inspection/unknown/request` ด้วย QoS 1
Dashboard จะบันทึกและส่ง JPEG เฟรมแรกจากกล้องฝั่งรับของหนึ่งภาพต่อคิวที่มี Unknown
รวมถึงคิวผสม เมื่อบันทึกภาพสำเร็จจะปิดกล้องและแสดงสถานะ “รอทีมตรวจ Unknown”
ภาพเปิดดูได้จากประวัติคิว

```json
{
  "schema_version": 1,
  "type": "unknown_review_request",
  "status": "awaiting_review",
  "request_id": "queue:43",
  "job_id": "pick-101",
  "armbot_job_id": "19",
  "event_id": 31,
  "captured_at": "2026-11-22T10:00:30+00:00",
  "requested": {"Unknown": 1},
  "image": {
    "content_type": "image/jpeg",
    "encoding": "base64",
    "data": "<JPEG encoded as Base64>"
  }
}
```

ทีมตรวจใช้ `request_id` กรองข้อความซ้ำจากการส่งแบบ QoS 1 ถอด `image.data` เป็น JPEG
และใช้ `job_id` หรือ `armbot_job_id` จับคู่กับคิวหยิบ ควรระบุ `job_id` ตั้งแต่ตอนส่งคิว
เพื่อให้ทั้งสองฝั่งใช้รหัสเดียวกัน ภาพจะถูกส่งด้วย `retain=false`
หากต้องการให้ผลจากทีมตรวจเปลี่ยนสถานะใน Dashboard ให้ตกลง topic และรูปแบบข้อความตอบกลับ
โดยใช้ `request_id` เดิม แล้วเพิ่มตัวรับผลใน Dashboard

## Deploy AWS ครั้งแรก

สร้าง EC2 ด้วย Ubuntu 24.04 x86 และเลือก user data จากไฟล์ `infra/cloud-init-aws.yaml` เพื่อให้ติดตั้ง Docker อัตโนมัติ จากนั้นเปิด Security Group เฉพาะพอร์ต `22`, `80`, `443` และผูก Elastic IP เข้ากับ instance

ตั้งค่า GitHub repository secrets:

- `AWS_HOST`: Elastic IP ของ EC2
- `AWS_USER`: `ubuntu`
- `AWS_SSH_PRIVATE_KEY`: private key ของ EC2 key pair
- `DOMAIN`: เช่น `12.34.56.78.nip.io` โดยแทนด้วย Elastic IP จริง หรือใช้โดเมนของคุณ
- `POSTGRES_PASSWORD`: รหัส PostgreSQL ที่ยาวและสุ่ม
- `ARDUINO_API_KEY`: API key ที่จะใส่ใน Arduino
- `MQTT_HOST`, `MQTT_PORT`, `MQTT_TRANSPORT`, `MQTT_TLS` และ `MQTT_WS_PATH`: ไม่จำเป็น ค่าเริ่มต้นคือ `broker.hivemq.com:8884`, `websockets`, TLS และ `/mqtt`
- `MQTT_USERNAME` และ `MQTT_PASSWORD`: credential ของ broker ส่วนตัว (ค่าเริ่มต้น HiveMQ ไม่ต้องใช้)

จากนั้น workflow `.github/workflows/deploy-aws.yml` จะทำงานอัตโนมัติ:

- ตรวจสอบ Python และ Docker Compose
- ส่ง source code ไปยัง EC2
- build และ restart Dashboard, PostgreSQL และ Caddy
- เปิด HTTPS ผ่าน Caddy
- ตรวจสอบ `/api/status` หลัง deploy

หน้าเว็บ cloud ต้องเปิดผ่าน HTTPS เพื่อให้เบราว์เซอร์อนุญาตกล้อง

## Train model

```powershell
py -m pip install -r requirements.txt
py train.py --epochs 100 --patience 100 --name iot-components-v4
```

สคริปต์อ่าน `datasets/data.yaml` และใช้ split `train`, `valid` และ `test`
ตรวจคู่ภาพกับ label และแปลง polygon annotations เป็นกรอบ bounding box สำหรับ
YOLO detection โดยเก็บทั้งสี่คลาส รวม `QR` ไว้ให้โมเดลเรียนรู้แยกจากอุปกรณ์
คลาสเรียงเป็น `QR`, `relay_module`, `triple_syringe` และ `ultrasonic_hc-sr04`;
สคริปต์ remap ID ตามชื่อคลาสจาก YAML อัตโนมัติ แอปไม่นับ QR เป็นอุปกรณ์ และ
ไม่นำกรอบ QR ไปนับเป็นวัตถุ `Unknown` ชุด `test` จะไม่ถูกใช้ฝึกหรือเลือก checkpoint
แต่ใช้ประเมิน best checkpoint หลังฝึก ค่าเริ่มต้นฝึก 100 epochs,
ขนาดภาพ 512 และหยุดก่อนกำหนดเมื่อ validation ไม่ดีขึ้น 20 epochs
หากต้องการให้ฝึกครบตามจำนวน epochs ที่กำหนด ให้ตั้ง `--patience` เท่ากับ `--epochs`

สำหรับโหมดทดสอบการแยกวัตถุออกจากฉากหลัง ให้เลือกกล้อง เปิดกล้องทดสอบขณะพื้นที่วางของว่าง แล้วกด
“บันทึกฉากหลังว่าง” ภาพอ้างอิงเก็บไว้ที่ `data/background_reference.jpg`
และใช้ได้ต่อหลังรีสตาร์ต โดยใช้กล้องตัวเดิมและให้ตำแหน่ง/แสงคงที่ หากเปลี่ยนกล้อง
มุมกล้องหรือฉากให้บันทึกภาพอ้างอิงใหม่ วัตถุสีหรือรูปร่างที่กลืนกับพื้นหลังอาจตรวจไม่พบ
และเงาหรือแสงที่เปลี่ยนอาจถูกนับเป็นวัตถุได้ ระบบตัดพื้นที่ที่โมเดลตรวจเป็นสามคลาสหลัก
ออกจาก foreground ในโหมดทดสอบกล้อง รวมถึง QR ที่โมเดลตรวจพบ

ภาพ train ปัจจุบันมีพื้นหลังสีต่างจากฉากขาวอมเทาที่ใช้งานจริง โมเดลสามคลาสหลัก
อาจตรวจพลาดเมื่อเปลี่ยนฉาก ควรเพิ่มภาพของทั้งสามคลาสที่ถ่ายด้วยกล้องและฉากจริง
แล้วฝึกใหม่เพื่อให้ผลตรวจตรงกับสภาพใช้งาน

แอปและ `detect.py` ใช้โมเดล `best2.pt` ที่ root ของโปรเจกต์ โมเดลที่ฝึกมีคลาส QR
และอุปกรณ์สามชนิด แต่แอปนับเฉพาะ Relay module, Triple syringe และ Ultrasonic HC-SR04
หากฝึกโมเดลใหม่และตรวจสอบผลแล้ว ให้นำ
`runs/object-detection/<ชื่อ run>/weights/best.pt` ไปตั้งเป็น `best2.pt` ก่อน build หรือ deploy

## หยุดระบบ local

```powershell
docker compose down
```

คำสั่งนี้ยังเก็บ PostgreSQL volume และ snapshot ไว้ ถ้าต้องการลบข้อมูล local ทั้งหมดจึงค่อยใช้ `docker compose down -v`

## ข้อจำกัดของข้อมูล

ชุดข้อมูลปัจจุบันมี 233 ภาพ (train 204, validation 19, test 10) โดย validation
และ test ยังมีขนาดเล็ก และ `triple_syringe` มีตัวอย่างน้อยกว่าคลาสอื่น
โมเดลจึงเหมาะสำหรับต้นแบบ ควรเพิ่มภาพจากฉาก กล้อง และสภาพแสงจริง โดยเฉพาะ
ตัวอย่าง `triple_syringe` และแยกตามภาพต้นฉบับก่อนใช้งานจริง
