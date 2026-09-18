"""
Account Manager - Unified account storage, export, and migration (SQLite-backed)
"""
import os
import csv
import json
import logging
from datetime import datetime
from core.database import DatabaseManager
from core.secret_safety import safe_account_metadata_rows, SAFE_ACCOUNT_EXPORT_FIELDS

logger = logging.getLogger('gmail_creator_accounts')


class AccountManager:
    def __init__(self, db_path=None):
        self.db = DatabaseManager(db_path) if db_path else DatabaseManager()

    def save(self, email, password, first_name="", last_name="",
             proxy="", strategy="", sms_service="", phone_number="",
             birthday="", gender="", status="active", notes="", profile_path="",
             profile_id="", engine="", profile_state="", identity_state="",
             browser_status="", mailbox_status="", overall_status="",
             browser_checked_at="", mailbox_checked_at="", last_error_code="",
             registration_result=None, warm_result=None):
        return self.db.save_account(
            email=email, password=password,
            first_name=first_name, last_name=last_name,
            proxy=proxy, strategy=strategy,
            sms_service=sms_service, phone_number=phone_number,
            birthday=birthday, gender=gender,
            status=status, notes=notes, profile_path=profile_path,
            profile_id=profile_id, engine=engine, profile_state=profile_state,
            identity_state=identity_state, browser_status=browser_status,
            mailbox_status=mailbox_status, overall_status=overall_status,
            browser_checked_at=browser_checked_at, mailbox_checked_at=mailbox_checked_at,
            last_error_code=last_error_code,
            registration_result=registration_result,
            warm_result=warm_result,
        )

    def get_all(self):
        return self.db.get_all_accounts()

    def get_count(self):
        accounts = self.db.get_all_accounts()
        return len(accounts)

    def get_stats(self):
        accounts = self.db.get_all_accounts()
        metadata_accounts = safe_account_metadata_rows(accounts)
        total = len(accounts)
        active = sum(1 for a in accounts if a.get("status") == "active")
        strategies = {}
        sms_services = {}
        for a in metadata_accounts:
            s = a.get("strategy", "unknown") or "unknown"
            strategies[s] = strategies.get(s, 0) + 1
            svc = a.get("sms_service", "") or ""
            if svc:
                sms_services[svc] = sms_services.get(svc, 0) + 1

        return {
            "total": total,
            "active": active,
            "success_rate": (active / total * 100) if total > 0 else 0,
            "strategies": strategies,
            "sms_services": sms_services,
        }

    def get_last_account(self):
        accounts = self.db.get_all_accounts()
        return accounts[0] if accounts else None

    def export_csv(self, filepath=None):
        if not filepath:
            filepath = f"data/accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        accounts = safe_account_metadata_rows(self.db.get_all_accounts())
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(list(SAFE_ACCOUNT_EXPORT_FIELDS))
            for acc in accounts:
                writer.writerow([acc.get(key, "") for key in SAFE_ACCOUNT_EXPORT_FIELDS])
        logger.info(f"Exported {len(accounts)} accounts to CSV: {filepath}")
        return filepath

    def export_json(self, filepath=None):
        if not filepath:
            filepath = f"data/accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        accounts = safe_account_metadata_rows(self.db.get_all_accounts())
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        logger.info(f"Exported {len(accounts)} accounts to JSON: {filepath}")
        return filepath

    def export_txt(self, filepath=None):
        if not filepath:
            filepath = f"data/accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        accounts = safe_account_metadata_rows(self.db.get_all_accounts())
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            for acc in accounts:
                f.write(f"{acc.get('email', '')}\n")
        logger.info(f"Exported {len(accounts)} accounts to TXT: {filepath}")
        return filepath

    def migrate_old_data(self):
        migrated = 0

        # Migrate accounts.txt
        txt_path = "data/accounts.txt"
        if os.path.exists(txt_path):
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and ":" in line:
                            parts = line.split(":")
                            email = parts[0]
                            password = parts[1] if len(parts) > 1 else ""
                            if self.db.save_account(email=email, password=password):
                                migrated += 1
                logger.info(f"Migrated {migrated} accounts from accounts.txt")
            except Exception as e:
                logger.error("TXT migration failed: %s", type(e).__name__)

        # Migrate accounts.json
        json_path = "data/accounts.json"
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                count = 0
                for acc in data:
                    if isinstance(acc, dict) and "email" in acc:
                        try:
                            profile = self.db.migration_profile_projection(acc)
                            if self.db.save_account(
                                email=acc.get("email", ""),
                                password=acc.get("password", ""),
                                first_name=acc.get("first_name", ""),
                                last_name=acc.get("last_name", ""),
                                proxy=acc.get("proxy", ""),
                                strategy=acc.get("strategy", ""),
                                sms_service=acc.get("sms_service", ""),
                                phone_number=acc.get("phone_number", ""),
                                birthday=acc.get("birthday", ""),
                                gender=acc.get("gender", ""),
                                status=acc.get("status", "active"),
                                notes=acc.get("notes", ""),
                                **profile,
                                browser_checked_at=acc.get("browser_checked_at", ""),
                                mailbox_checked_at=acc.get("mailbox_checked_at", ""),
                                last_error_code=acc.get("last_error_code", ""),
                            ):
                                count += 1
                        except Exception as exc:
                            logger.warning("Skipping invalid JSON account row: %s", type(exc).__name__)
                migrated += count
                logger.info(f"Migrated {count} accounts from accounts.json")
            except Exception as e:
                logger.error("JSON migration failed: %s", type(e).__name__)

        return migrated


account_manager = AccountManager()
