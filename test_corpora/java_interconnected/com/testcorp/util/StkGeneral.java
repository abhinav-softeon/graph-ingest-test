package com.testcorp.util;

public final class StkGeneral {
    private StkGeneral() {}

    public static String nullCheck(String v) {
        return v == null ? "" : v;
    }

    public static String[] getStringArray(String v) {
        return nullCheck(v).split(",");
    }

    public static boolean isEmpty(String v) {
        return nullCheck(v).length() == 0;
    }
}
