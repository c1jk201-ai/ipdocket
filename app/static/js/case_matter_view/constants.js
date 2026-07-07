export const DOC_TYPE_LABELS = {
 OFFICE_ACTION: "OA/",
 SPEC: "people/",
 POA: "",
 INVOICE: "Billing",
 RECEIPT: "",
 TRANSLATION: "",
 EVIDENCE: "",
 OTHER: "Other",
};

export const AUDIT_ACTION_LABELS = {
 PATCH: " Change",
 UNDO: "Undo",
 USER: "User",
 UPLOAD: "Upload",
 MEMO: "Notes",
 FILE: "File",
 DEADLINE: "Deadline",
 SYSTEM: "SYSTEM",
 AI: "AI",
};

export const AUDIT_ACTION_ORDER = [
 "PATCH",
 "UNDO",
 "UPLOAD",
 "MEMO",
 "FILE",
 "DEADLINE",
 "USER",
 "SYSTEM",
 "AI",
];

export const AUDIT_FIELD_LABELS = {
 title: "Matterpeople",
 assignee_id: "Contact",
 client_id: "Client",
 status: "Status",
 our_ref: "Our Ref",
 your_ref: "Your Ref",
 "status.inhouse_status": "status change",
 "memo.add": "Notes Add",
 "memo.delete": "Notes Delete",
 "progress.add": "Progress Add",
 "progress.delete": "Progress Delete",
 "deadline.add": "Deadline Add",
 "file.doc_type": "File Type Change",
 "fm.upload": "File Upload",
 "fm.folder.create": "Folder ",
 "fm.move": "File/Folder Go",
 "fm.delete": "File/Folder Delete",
 registry_image: "Image Change",
 "matter.delete": "Matter Delete",
 "history.notice.create": "Office correspondence Registration",
 "history.notice.update": "Office correspondence Edit",
 "history.notice.delete": "Office correspondence Delete",
};

export const CUSTOM_TEXT_LABELS = {
 priority: "Priority Notes",
 license: "",
 transfer: "Previous",
 progress_misc: "Open(Other)",
 progress: "Open",
 old_workflow: " Task",
};
