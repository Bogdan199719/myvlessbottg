# 🚀 MyVlessBot v2.4.1 - Quick Start Guide

## ⚡ 30 Second Summary

**What's New:**
- ✅ Automatic XTLS synchronization (fixes VPN connectivity issues)
- ✅ Fixed duplicate subscriptions (no more 2 servers)
- ✅ Continuous monitoring (every 5 minutes)

**Deploy In 2 Commands:**
```bash
cd /path/to/vless-shopbot
docker-compose up -d
```

**Verify In 10 Seconds:**
```bash
docker logs vless-shopbot | grep XTLS
```

---

## 📋 What Problems Does v2.4.1 Solve?

### Problem 1: VPN Doesn't Work After Panel Changes
**Before v2.4.1:**
- Change inbound protocol to Reality TCP in panel → VPN breaks
- Manual fix required

**After v2.4.1:**
- Automatic sync at startup
- Automatic sync every 5 minutes
- VPN works automatically ✅

### Problem 2: Users Get 2 Servers Instead of 1
**Before v2.4.1:**
- Download subscription → Get duplicate servers
- User confusion and app errors

**After v2.4.1:**
- Smart deduplication logic
- Always get exactly 1 server per host ✅

---

## 🐳 Deployment (Pick One)

### Option A: Docker Compose (Recommended)
```bash
cd /path/to/vless-shopbot
git pull origin main
docker-compose down
docker-compose up -d
```

### Option B: Docker Direct
```bash
docker build -t vless-shopbot:2.4.1 .
docker stop vless-shopbot || true
docker rm vless-shopbot || true
docker run -d \
  --name vless-shopbot \
  --restart unless-stopped \
  -p 1488:1488 \
  -v $(pwd)/users.db:/app/project/users.db \
  -v $(pwd)/.env:/app/project/.env \
  vless-shopbot:2.4.1
```

### Option C: Manual (Not Recommended)
```bash
cd /path/to/vless-shopbot
git pull origin main
python -m shop_bot
```

---

## ✅ Verify Deployment (Copy & Paste)

```bash
# Check logs
docker logs vless-shopbot -f | grep -E "(XTLS|Startup|Periodic)"

# You should see within 30 seconds:
# Performing initial XTLS synchronization at startup...
# XTLS sync completed for host 'Riga'
# XTLS sync completed for host 'USA'
```

**Expected Output Example:**
```
Performing initial XTLS synchronization at startup...
XTLS sync for 'Riga' - protocol: vless, network: tcp, security: reality
Fixed XTLS for client 'user123@example.com': flow='xtls-rprx-vision'
XTLS sync for 'Riga': 1 clients fixed
XTLS sync for 'USA' - protocol: vless, network: tcp, security: tls
XTLS sync for 'USA': 0 clients fixed
Startup XTLS synchronization completed: 1 total clients fixed
```

---

## 🧪 Quick Test (5 Minutes)

### Test 1: Does XTLS sync work?
```bash
# Wait 5 minutes after startup
sleep 300

# Check logs for periodic sync
docker logs vless-shopbot | grep "Starting periodic XTLS sync"

# Should find: "Starting periodic XTLS synchronization"
```

### Test 2: Do subscriptions work?
```bash
# Download a subscription
curl "http://localhost:1488/sub/YOUR_TOKEN" -o sub.txt

# Count lines (each server = 1 line)
wc -l sub.txt

# Should be: number of hosts, not more
# (e.g., 3 hosts = 3 lines)
```

### Test 3: Try Reality TCP
```bash
# In your 3xui panel:
# 1. Create/modify inbound to "VLESS Reality TCP"
# 2. Add a test client
# 3. Either restart bot or wait 5 minutes
# 4. In panel, check client flow
# 5. Should show: flow = "xtls-rprx-vision"
```

---

## 📊 What Changed (For Developers)

### Files Modified (5):
1. `src/shop_bot/modules/xui_api.py` - Added XTLS sync function
2. `src/shop_bot/data_manager/scheduler.py` - Added periodic task
3. `src/shop_bot/__main__.py` - Added startup sync
4. `src/shop_bot/webhook_server/subscription_api.py` - Enhanced dedup
5. `src/shop_bot/version.py` - 2.4.0 → 2.4.1

### Lines Added: ~250 lines of code
### Lines Removed: ~8 lines of code

---

## 🔍 Monitoring (Log Watching)

### Watch XTLS Sync Operations
```bash
docker logs -f vless-shopbot | grep -i xtls
```

### Watch Subscription Dedup Operations
```bash
docker logs -f vless-shopbot | grep -i dedup
```

### Watch All Errors
```bash
docker logs -f vless-shopbot | grep -i error
```

### Watch Everything
```bash
docker logs -f vless-shopbot
```

---

## 🚨 If Something Goes Wrong

### Symptom: No XTLS messages in logs
**Fix:**
```bash
# Check if xui_hosts are configured
docker exec vless-shopbot sqlite3 users.db "SELECT * FROM xui_hosts;"

# Should see your panel URLs
# If empty: Configure hosts in panel
```

### Symptom: Still seeing duplicate servers
**Fix:**
```bash
# Check database for duplicates
docker exec vless-shopbot sqlite3 users.db \
  "SELECT host_name, COUNT(*) FROM vpn_keys GROUP BY host_name HAVING COUNT(*) > 1;"

# If duplicates found: Report issue with output above
```

### Symptom: High CPU/Memory usage
**Fix:**
```bash
# Check logs for errors
docker logs vless-shopbot | grep -i error

# Restart if needed
docker-compose restart vless-shopbot
```

### Symptom: Panel connection errors
**Fix:**
```bash
# Test panel connectivity
docker exec vless-shopbot curl -I https://your-panel-url/

# Should return: HTTP 200 or 301 (not connection refused)
```

---

## 📞 Support

**Issue with XTLS sync?**
1. Check logs: `docker logs vless-shopbot | grep XTLS`
2. Verify panel URL in database
3. Test panel connectivity manually
4. Check panel credentials

**Issue with subscriptions?**
1. Check logs: `docker logs vless-shopbot | grep [email protected]`
2. Download subscription file
3. Count servers (should match number of hosts)
4. Check for "Dedup" messages

**General questions?**
- Read: `RELEASE_SUMMARY_v2.4.1.md`
- Read: `IMPLEMENTATION_SUMMARY_v2.4.1.md`
- Read: `COMPLETE_IMPLEMENTATION_REPORT.md`

---

## 🔄 Rollback (If Critical Issue)

```bash
git checkout v2.4.0
docker-compose up -d
```

---

## ✨ What's Next?

### For Users:
- Better VPN reliability
- No more subscription issues
- Automatic problem fixing

### For You:
- Monitor XTLS sync logs
- No configuration needed
- Report any issues

### For Future:
- v2.5.0: Configurable sync intervals
- v2.5.0: Admin dashboard
- v2.5.0: Notification system

---

## 📚 Full Documentation

- **CHANGELOG_v2.4.1.md** - Detailed changelog
- **RELEASE_SUMMARY_v2.4.1.md** - Release notes
- **IMPLEMENTATION_SUMMARY_v2.4.1.md** - Implementation guide
- **COMPLETE_IMPLEMENTATION_REPORT.md** - Full technical report

---

## ✅ Ready to Go!

Your MyVlessBot v2.4.1 is ready to:
- ✅ Sync XTLS automatically
- ✅ Fix subscriptions
- ✅ Work reliably
- ✅ Monitor itself
- ✅ Log everything

**Status**: Ready for Production
**Risk**: Low
**Support**: Available

**Deploy now and enjoy better reliability!** 🎉
