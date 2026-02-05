# MyVlessBot v2.4.0 Release Notes

## Overview
This release focuses on **security improvements**, **race condition prevention**, and **reliability enhancements** to ensure stable operation in production environments.

## Major Improvements

### 1. Race Condition Protection ✅
**Problem**: Users could double-click "buy" button and receive two VPN keys for one payment.  
**Solution**: Added `pending_payment` flag in database that blocks concurrent payment processing.

**Implementation**:
- Added `pending_payment BOOLEAN` column to users table (migration safe)
- New functions: `set_pending_payment()`, `get_pending_payment_status()`, `clear_all_pending_payments()`
- Protected `process_successful_payment()` to prevent duplicate key issuance

**Files Modified**:
- `src/shop_bot/data_manager/database.py` - Added migration + helper functions
- `src/shop_bot/bot/handlers.py` - Integrated race condition protection in payment processor

### 2. Panel Unavailability Fallback 🔄
**Problem**: When 3x-ui panel goes offline, `/sub/<token>` endpoint returns empty subscription.  
**Solution**: Added fallback mechanism that regenerates config from cached database data.

**Implementation**:
- Modified `subscription_api.py` to attempt regeneration via `xui_api.get_key_details_from_host()` 
- Graceful degradation: If panel unavailable, uses cached `connection_string` from DB
- Improved logging for debugging connectivity issues

**Files Modified**:
- `src/shop_bot/webhook_server/subscription_api.py` - Added fallback generation logic

### 3. Database Performance Optimization 📊
**Problem**: Slow queries when processing 1000+ users during subscription checks.  
**Solution**: Added 4 strategic indexes on frequently-queried columns.

**Indexes Added**:
- `idx_vpn_keys_user_id` - Fast user key lookup
- `idx_vpn_keys_expiry` - Fast expiry date filtering 
- `idx_transactions_user_id` - Fast transaction history retrieval
- `idx_users_banned` - Fast ban status checks

**Database**:
- Safe migrations using `ALTER TABLE` (backwards compatible)
- All changes preserve existing data

### 4. Enhanced Security & Git Protection 🔐
**Problem**: Production database and secrets could accidentally be pushed to GitHub.  
**Solution**: Comprehensive `.gitignore` update

**Protected Files**:
- `users.db*` - Production database (prevents data leaks)
- `.env*` - API keys and secrets
- Developer documentation: `ARCHITECTURE.md`, `DEVELOPER_GUIDE.md`, etc.

### 5. Improved Error Handling 🛡️
- Stale `pending_payment` flags cleared on bot startup
- Better error recovery with flag cleanup on exceptions
- Enhanced logging throughout payment processing pipeline

## Version Information
- **Previous Version**: 2.3.7
- **Current Version**: 2.4.0
- **Release Date**: 2024
- **Git Tag**: `v2.4.0`

## Database Migration
All database changes are **backwards-compatible** and automatically applied on startup:
- Safe `ALTER TABLE ADD COLUMN` operations
- No data loss or destructive changes
- Existing data preserved

## Deployment Instructions

### For Ubuntu Server (Docker)

1. **Pull Latest Changes**:
   ```bash
   cd /path/to/vless-shopbot
   git pull origin main
   ```

2. **Verify Web Panel Auto-Update**:
   - The web panel will detect v2.4.0 availability automatically
   - Click "Update" in Admin Panel → System → Updates
   - Or restart container to apply changes

3. **Verify Changes**:
   ```bash
   docker-compose logs -f shop-bot  # Check for successful startup
   ```

### For Manual Testing

1. **Backup Current Database** (Important!):
   ```bash
   cp users.db users.db.backup
   ```

2. **Apply Changes**:
   ```bash
   git fetch origin
   git checkout v2.4.0  # Or pull main
   ```

3. **Restart Bot**:
   ```bash
   docker-compose restart
   ```

## Testing Checklist

- [ ] Bot starts without errors
- [ ] Database migrations apply successfully
- [ ] User can complete payment without duplicate key issuance
- [ ] Subscription tokens still generate correctly
- [ ] Panel offline scenario: `/sub/<token>` still returns configs
- [ ] Admin panel shows v2.4.0 in System → About

## Troubleshooting

### If Migrations Fail
1. Check logs: `docker-compose logs shop-bot`
2. Restore from backup: `cp users.db.backup users.db`
3. Contact support with logs

### If Payment Processing Stalls
1. Restart container: `docker-compose restart`
2. Clear pending flags manually (if needed):
   ```python
   python -c "from src.shop_bot.data_manager.database import clear_all_pending_payments; clear_all_pending_payments()"
   ```

## Security Notes

✅ **No Secrets in Repository**: All `.env`, database files properly excluded  
✅ **Data Protection**: User database never committed to Git  
✅ **API Keys Safe**: Configuration secrets protected via `.env`  

## Files Modified

```
.gitignore
pyproject.toml
src/shop_bot/__main__.py
src/shop_bot/version.py
src/shop_bot/bot/handlers.py
src/shop_bot/data_manager/database.py
src/shop_bot/webhook_server/subscription_api.py
```

## Support

For issues or questions:
1. Check logs in Admin Panel → System → Logs
2. Review database status: `SELECT COUNT(*) FROM users;`
3. Contact support with version info: `v2.4.0`

---

**MyVlessBot Development Team**  
Repository: https://github.com/Bogdan199719/myvlessbottg
