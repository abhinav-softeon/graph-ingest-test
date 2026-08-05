package com.testcorp.util;

public final class AuditLog {
    private AuditLog() {}

    public static void record(String who, String what) {
        String w = StkGeneral.nullCheck(who);
        System.out.println("[audit] " + w + " " + StkGeneral.nullCheck(what));
    }
}
