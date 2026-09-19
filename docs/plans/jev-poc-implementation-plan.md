# แผน implementation: Jev เป็น internal tool ของ harness

**สถานะ:** implementation อยู่บน branch ปัจจุบัน; รอ human review และ live evaluation ก่อนยืนยัน quality gate
**ขอบเขตเอกสาร:** บันทึก implementation ตามข้อ 1; ยังไม่มีผลประเมินจาก API จริง

## เป้าหมาย

เพิ่ม Jev เป็นตัวจัดประเภทคำขอแบบมีโครงสร้างที่ `NativeHarness` เรียกใช้ได้เมื่อผู้ใช้เปิดใช้งาน โดยให้ LLM หลักยังเป็นผู้คุยกับผู้ใช้ ใช้ skills/MCP tools และสร้างคำตอบในหน้า Chat เดิม

```text
Chat → agent service → NativeHarness
                         ├─ Jev ปิด: ใช้ flow ปัจจุบัน
                         └─ Jev เปิด: จัดประเภทคำขอ → เลือกเส้นทาง
                                                ├─ BA/Summarizer skill → LLM หลัก + tools
                                                └─ เส้นทางทั่วไป → LLM หลัก + tools
```

รายละเอียดเหตุผลและผลประเมิน provider อยู่ใน [ADR 0004](../adr/0004-typesafe-jev-internal-harness-tool-proposal.md)

## พฤติกรรม POC ที่กำหนด

- ต่อ `POST https://api.typesafe.ai/v1/systemone` โดยตรง ใช้ `httpx` ที่มีใน backend อยู่แล้ว ไม่ใช้ OpenRouter และไม่เพิ่ม TypeSafe Python SDK
- ใช้ model ID ที่ pin ระหว่าง POC: `jev-1.13.0`; UI แสดง model นี้ แต่ยังไม่เปิดให้เปลี่ยนรุ่นระหว่างการประเมิน
- คำถามแรกให้ Jev เลือกประเภทจากชุดค่าคงที่: `business_analysis`, `summarize_sources`, `general_question`, `needs_clarification` และคืน confidence
- route `business_analysis` ไป skill `ba`, `summarize_sources` ไป skill `summarizer` เฉพาะเมื่อ confidence ถึง threshold ชั่วคราว `0.95` และ skill ใช้ได้ในโครงการนั้น; threshold นี้ยังไม่ได้รับการยืนยันจาก live evaluation
- `general_question`, `needs_clarification`, confidence ต่ำ, ผลลัพธ์ผิดรูปแบบ หรือ Jev ใช้งานไม่ได้ ให้กลับไป flow เดิมของ harness เพื่อให้ LLM หลักตอบหรือถามข้อมูลเพิ่ม
- ถ้าผู้ใช้เลือก skill เอง หรือพิมพ์คำสั่ง `/skill ...` ให้ข้าม Jev และคงพฤติกรรมเดิม
- เมื่อ toggle ปิด ห้ามมี request ไป TypeSafe ระหว่างการคุย
- ใน POC ส่งเฉพาะข้อความคำขอและ metadata ที่จำเป็นต่อการจัดประเภท ไม่ส่งเนื้อหาเอกสารทั้งฉบับให้ Jev

## งาน implementation ตามลำดับ

### 1. เพิ่มโมดูล Jev แยกจาก LLM chat client

ไฟล์ใหม่ที่เสนอ: `backend/app/jev/client.py` และ `backend/app/jev/routing.py`

- `JevDecisionClient.classify(state)` ส่งคำถาม intent แบบ fixed schema และคืนชนิดข้อมูลที่ระบบกำหนด ไม่ implement interface `LLMClient` และไม่แปลง Jev ให้เป็น chat completion
- client เรียก TypeSafe endpoint ด้วย Bearer key, model ที่ pin และ schema `state + questions`
- ตรวจ HTTP status, timeout, response schema, category ที่อนุญาต และช่วง confidence ก่อนคืนผล
- จัดการ timeout และ rate limit (`429`/`529`) ด้วย retry ที่จำกัดจำนวน; ข้อผิดพลาดต้องไม่ทำให้ chat turn ล้ม
- รองรับ injected HTTP transport เพื่อทดสอบ API contract ด้วย mock โดยไม่เรียกบริการจริง
- routing module กำหนด mapping จาก category ไป skill ID และ threshold; ไม่ให้ output จากโมเดลสร้างชื่อ tool, skill หรือ action เอง

### 2. เพิ่มการตั้งค่าและจัดเก็บ key

ไฟล์หลัก: `backend/app/db.py`, โมดูลตั้งค่าใน `backend/app/jev/` และ `backend/app/secrets.py` เฉพาะจุดเรียกใช้

- ใช้ตาราง `settings` แบบ key-value ที่มีอยู่แล้ว ไม่เพิ่มตารางหรือ migration schema ใหม่
- เพิ่มค่า `internal_tools.jev.enabled=false` สำหรับฐานข้อมูลใหม่และฐานข้อมูลเดิมที่ยังไม่มี key นี้
- เก็บ credential แยกภายใต้ secret reference เช่น `secret://internal-tools/jev`; API อ่านคืนได้แค่ `has_api_key`/ค่าปกปิด ไม่คืน key จริง
- การเอา key ออกต้องปิด Jev ก่อน; หากลบ secret ไม่สำเร็จ API ต้องรายงานความล้มเหลวและไม่แสดงว่าสำเร็จ
- ใช้ `SecretStore` ของแอปสำหรับ POC และอธิบายใน Settings ว่าเป็น local storage ตามขอบเขตที่ระบุใน ADR

### 3. เพิ่ม Settings API

ไฟล์หลัก: `backend/app/api/settings.py`

- `GET /api/settings/internal-tools/jev` คืน enabled, has_api_key, model ID และสถานะ readiness โดยไม่คืน secret
- `PUT /api/settings/internal-tools/jev` บันทึก enabled และ key แบบ write-only; รองรับ `clear_api_key` และป้องกันการเปิดใช้เมื่อไม่มี key
- `POST /api/settings/internal-tools/jev/test` ส่งคำขอทดสอบสั้น ๆ ที่ไม่มีข้อมูลโครงการ และคืน reachable/model/latency/error แบบไม่เปิดเผย key
- ทดสอบ connection ได้แม้ toggle ปิด เพราะเป็นการกระทำที่ผู้ใช้กดเอง; การส่งข้อความ Chat ขณะปิดต้องไม่เรียก API

### 4. เชื่อม service เข้ากับ NativeHarness

ไฟล์หลัก: `backend/app/agent/service.py`, `backend/app/agent/harness.py` และ `HarnessRequest`

- ให้ `agent/service.py` อ่าน toggle และ secret แล้วส่ง optional Jev client เข้า `HarnessRequest`; harness ไม่ต้องรับผิดชอบการอ่าน secret store
- ให้ `NativeHarness` เรียก Jev ก่อนสร้าง run record เพื่อให้ `selected_skill` ใน run สอดคล้องกับเส้นทางที่เลือก
- ใช้ Jev เฉพาะเมื่อ enabled, มี key, ไม่มี explicit skill และยังอยู่บน native harness
- route ไป skill เฉพาะ category ที่กำหนด, confidence ผ่าน threshold, และ skill เปิดใช้ได้กับโครงการ; กรณีอื่นปล่อยให้ flow ปัจจุบันเลือก tools/skills
- บันทึก metadata ที่จำเป็นสำหรับประเมินผล เช่น model/version, category, confidence, latency และ route โดยไม่บันทึก prompt/state เต็มหรือ API key
- ถ้า Jev timeout, rate limit, schema ไม่ถูกต้อง หรือ confidence ต่ำ ให้บันทึกเหตุการณ์ที่ไม่มีข้อมูลอ่อนไหวและทำงานต่อผ่านเส้นทางเดิม

### 5. เพิ่มหน้าตั้งค่า Jev

ไฟล์หลัก: `frontend/app/settings/page.tsx`, `frontend/lib/api.ts` และ `frontend/components/ui/` หากต้องเพิ่ม component

- เพิ่มส่วน **Internal tools → Jev** ในหน้า Settings เดิม
- ใช้ shadcn/ui components ที่มีในโปรเจกต์; เพิ่ม Switch หากยังไม่มี component ที่เหมาะสม
- แสดง toggle, สถานะ API key, ช่องใส่/เปลี่ยน key แบบ password, ปุ่ม Test connection และ model ID แบบอ่านอย่างเดียว
- แสดงข้อความสั้น ๆ ว่าเมื่อเปิดใช้ ข้อความคำขอจะถูกส่งไป TypeSafe เพื่อจัดประเภท
- แสดง error ของ credential/connection และสถานะ enabled แยกกัน; การทดสอบ connection ไม่ได้เปิดใช้งาน Jev ให้อัตโนมัติ

## แผนทดสอบและเกณฑ์ผ่าน

### Backend tests

- การตั้งค่าเริ่มต้นเป็นปิด ทั้งฐานข้อมูลใหม่และฐานข้อมูลเดิม
- secret key ถูก mask ใน GET/PUT/test response และไม่อยู่ใน log, export, chat history หรือ error response
- client ส่ง endpoint, Authorization header และ request schema ตาม TypeSafe API; validate response และจัดการ timeout/429/529
- disabled chat, manual skill invocation และ explicit `/skill` ไม่เรียก Jev
- เมื่อเปิดใช้ Jev จัด route ไป `ba`/`summarizer` ได้เฉพาะคำตอบที่อยู่ใน allowlist และ confidence ผ่าน threshold
- category ทั่วไป, `needs_clarification`, confidence ต่ำ, skill ปิดใช้, response ผิดรูปแบบ และ API failure กลับสู่ flow เดิม
- ใช้ mock transport ตรวจว่าไม่มี outbound Jev request ระหว่าง chat เมื่อปิด toggle

### Frontend checks

- โหลดและบันทึกสถานะ toggle ได้; key เดิมไม่แสดงกลับในช่องหลังบันทึก
- ทดสอบ connection ได้โดยไม่เปลี่ยน enabled state
- แสดง readiness, error และคำอธิบายการส่งข้อมูลออกไป TypeSafe ชัดเจน

### Evaluation ก่อนเปิดใช้กับงานจริง

- ทำชุดคำขอ label โดยคนตรวจ แยกภาษาไทย/อังกฤษและประเภทงาน พร้อมเทียบกับ baseline ของ harness ปัจจุบัน
- วัด precision/recall แยก category, false skill routing, coverage, calibration, latency, error rate และต้นทุน
- ตั้ง confidence threshold จากผล evaluation; เกณฑ์เสนอสำหรับ auto-route คือ precision อย่างน้อย 95% ในแต่ละ skill route และต้องรายงานผลภาษาไทยแยกจากภาษาอังกฤษ
- ถ้าไม่ผ่านเกณฑ์ ให้ปรับชุดคำถาม/threshold หรือคงการ auto-route ไว้ไม่เปิดใช้ จนกว่าจะประเมินซ้ำ

baseline ใน runner ให้ LLM หลักจัดประเภทข้อความด้วยคำสั่งและหมวดเดียวกับ Jev เพื่อเปรียบเทียบความแม่นยำของการจัดประเภท; ยังไม่ได้ replay harness/tool execution แบบเต็ม

ชุดตัวอย่างอยู่ที่ `backend/app/jev/evaluation_cases.json` และเริ่มต้นด้วย
`human_reviewed: false` จึงยังรายงาน quality gate เป็นไม่ผ่านจนกว่าจะมีคนตรวจ
label แล้วรันคำสั่งนี้จาก root ของ repository:

```bash
PYTHONPATH=backend .venv/bin/python -m app.jev.evaluation
```

คำสั่งส่งข้อความตัวอย่างไป TypeSafe และ LLM profile ปัจจุบันโดยตรง รายงานถูก
เขียนไป `data/jev-evaluation/latest.md` โดยเก็บเฉพาะ case IDs และผลทำนาย

## ลำดับส่งมอบ

1. โมดูล client/routing และ backend tests แบบ mock
2. settings persistence, API และ tests
3. harness integration พร้อม fallback และ tests
4. Settings UI พร้อมการจัดการ credential และ test connection
5. evaluation ภาษาไทย/อังกฤษ แล้วเปิด Jev เฉพาะเมื่อผ่านเกณฑ์

## ข้อจำกัดและการตัดสินใจที่ยังต้องยืนยันตอน implementation

- `SecretStore` ปัจจุบันเป็น local obfuscation ตามเอกสารในโค้ด ไม่ใช่ secret manager สำหรับ production หรือ multi-user deployment
- threshold สุดท้ายต้องได้จาก evaluation จริง; ค่า 95% เป็นเกณฑ์เสนอสำหรับ precision ของ auto-route ไม่ใช่ผลที่ยืนยันแล้ว
- หาก TypeSafe เปลี่ยน API schema หรือ model version ต้องทดสอบ contract และ evaluation ซ้ำก่อนเปลี่ยนรุ่นที่ pin
- แผนนี้ยังไม่รวม OpenRouter, การใช้ Jev ให้จัดประเภทเนื้อหาเอกสารทั้งชุด, การเปลี่ยน LLM หลัก หรือการให้ Jev เรียก MCP tools โดยตรง
