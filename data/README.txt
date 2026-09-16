═══════════════════════════════════════════════════════════════
  📁 مجلد البيانات - Data Directory
═══════════════════════════════════════════════════════════════

هذا المجلد يحتوي على البيانات:

📄 names.txt          - قائمة الأسماء (اسم واحد في كل سطر)
📄 accounts.json      - الحسابات القديمة للاستيراد (لا يتم استيرادها تلقائياً)
📄 database.db        - قاعدة SQLite الخاصة بالبيئة
📁 profiles/<id>/      - جلسة المتصفح الدائمة المرتبطة بـ profile_id العشوائي
                       profile_manifest.json و profile.lock وملفات Chromium

⚠️ مهم: يمكنك إضافة/تعديل الأسماء في names.txt!

ملفات profiles تحتوي على جلسات تسجيل الدخول وبيانات حساسة. لا تعيد تسمية المجلدات
ولا تنقلها بين بيئات dev/prod. يستخدم warmer و health_checker المحرك المسجل في
profile_manifest.json فقط، ويحصلان على قفل حصري قبل فتح المتصفح.

Legacy directories without a manifest are reported as `legacy_unbound`; they are
not auto-adopted. Back up the complete environment (`database.db` and
`profiles/`) before migration or recovery.

═══════════════════════════════════════════════════════════════

