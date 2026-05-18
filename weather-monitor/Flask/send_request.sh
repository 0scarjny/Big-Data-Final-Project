#curl -X POST http://127.0.0.1:8080/send-to-bigquery \
#-H "Content-Type: application/json" \
#-d '{
#  "passwd": "<YOUR_PSWD>",
#  "values": {
#    "date": "2025-03-13",
#    "time": "16:30:00", 
#    "indoor_temp": 23,  
#    "indoor_humidity": 67
#  }
#}'

curl -X POST http://127.0.0.1:8080/get_outdoor_weather \
-H "Content-Type: application/json" \
-d '{"passwd": "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4", "city": "Paris"}'

curl -X POST https://flask-app-868833155300.europe-west6.run.app/get_outdoor_weather \
-H "Content-Type: application/json" \
-d '{"passwd": "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4", "city": "Renens"}'

curl -X POST http://0.0.0.0:8080/voice-assistant/text \
  -H "Content-Type: application/json" \
  -H "X-Shared-Secret: 03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4" \
  -d '{"text":"What was the humidity yesterday?"}'

curl -X POST https://flask-app-868833155300.europe-west6.run.app/voice-assistant \
  -H "Content-Type: audio/wav" \
  -H "X-Shared-Secret: 03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4" \
  --data-binary @/Users/oscar/Documents/VS_Code/HEC_BigData/Big-Data-Final-Project/weather-monitor/test/what.wav \
  -D /tmp/headers.txt -o /tmp/reply.pcm

cat /tmp/headers.txt
#check reply
  cat /tmp/headers.txt | grep X-Response-Text
  ffplay -f s16le -ar 16000 -ch_layout mono /tmp/reply.pcm


# Implicit city → uses device_location
curl -s -X POST http://0.0.0.0:8080/voice-assistant/text \
  -H "Content-Type: application/json" \
  -H "X-Shared-Secret: 03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4" \
  -d '{"text":"Quelle sera la météo demain?", "language":"fr-FR", "device_location":"Lausanne"}' \
  | python3 -m json.tool

# Explicit city in the question → overrides device_location
curl -s -X POST http://0.0.0.0:8080/voice-assistant/text \
  -H "Content-Type: application/json" \
  -H "X-Shared-Secret: 03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4" \
  -d '{"text":"Est-ce qu'\''il va pleuvoir demain à Genève?", "language":"fr-FR", "device_location":"Lausanne"}' \
  | python3 -m json.tool


# --- Critical announcement (proactive, presence-triggered) ---

# presence context — local. Always returns 200 (greeting if nothing critical).
curl -X POST http://0.0.0.0:8080/critical-announcement \
  -H "Content-Type: application/json" \
  -H "X-Shared-Secret: 03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4" \
  -d '{"location":"Lausanne","indoor_temp":21.4,"indoor_humidity":48,"indoor_co2":1500,"context":"presence","language":"en-US"}' \
  -D /tmp/ann_headers.txt -o /tmp/ann_reply.pcm
cat /tmp/ann_headers.txt
ffplay -f s16le -ar 16000 -ch_layout mono -nodisp -autoexit /tmp/ann_reply.pcm

# morning_check context — returns 204 No Content when no rain is forecast,
# or 200 + an umbrella reminder when rain is on the way.
curl -X POST https://flask-app-868833155300.europe-west6.run.app/critical-announcement \
  -H "Content-Type: application/json" \
  -H "X-Shared-Secret: 03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4" \
  -d '{"location":"Lausanne","indoor_temp":20,"indoor_humidity":50,"indoor_co2":600,"context":"morning_check","language":"en-US"}' \
  -D /tmp/morning_headers.txt -o /tmp/morning_reply.pcm
cat /tmp/morning_headers.txt
ffplay -f s16le -ar 16000 -ch_layout mono -nodisp -autoexit /tmp/morning_reply.pcm