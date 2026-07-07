# Define the core permissions available in the system
class Permissions:
    MENU_CASES = "menu.cases"
    MENU_DEADLINES = "menu.deadlines"
    MENU_NOTICES = "menu.notices"
    MENU_RENEWAL = "menu.renewal"
    MENU_CRM = "menu.crm"
    MENU_ACCOUNTING = "menu.accounting"
    MENU_STATISTICS = "menu.statistics"
    MENU_MGMT = "menu.mgmt"
    MENU_ADMIN = "menu.admin"
    CASE_VIEW_ASSIGNED = "case.view.assigned"
    CASE_VIEW_TEAM = "case.view.team"
    CASE_VIEW_ALL = "case.view.all"
    CASE_EDIT_ASSIGNED = "case.edit.assigned"
    CASE_EDIT_TEAM = "case.edit.team"
    CASE_EDIT_ALL = "case.edit.all"
    CASE_ASSIGN_TEAM = "case.assign.team"
    CASE_ASSIGN_ALL = "case.assign.all"
    CASE_DELETE = "case.delete"
    INVOICE_MANAGE = "invoice.manage"

    @classmethod
    def all_permissions(cls):
        """Return a list of all permission tuples (key, description) for the UI."""
        return [
            (cls.MENU_CASES, "Matter Menu "),
            (cls.MENU_DEADLINES, "Task/Deadline Menu "),
            (cls.MENU_NOTICES, "Notice Menu "),
            (cls.MENU_RENEWAL, "RenewalDeadline Menu "),
            (cls.MENU_CRM, "Client Menu "),
            (cls.MENU_ACCOUNTING, "/Billing Menu "),
            (cls.MENU_STATISTICS, " Menu "),
            (cls.MENU_MGMT, "Operations Menu "),
            (cls.MENU_ADMIN, "Admin "),
            (cls.CASE_VIEW_ASSIGNED, "Matter Search:  Responsible"),
            (cls.CASE_VIEW_TEAM, "Matter Search:  Responsible"),
            (cls.CASE_VIEW_ALL, "Matter Search: All"),
            (cls.CASE_EDIT_ASSIGNED, "Matter Edit:  Responsible"),
            (cls.CASE_EDIT_TEAM, "Matter Edit:  Responsible"),
            (cls.CASE_EDIT_ALL, "Matter Edit: All"),
            (cls.CASE_ASSIGN_TEAM, "Matter Contact : "),
            (cls.CASE_ASSIGN_ALL, "Matter Contact : All"),
            (cls.CASE_DELETE, "Matter Delete"),
            (cls.INVOICE_MANAGE, "Invoice/ Process"),
        ]
