package com.testcorp.util;

public final class Validator {
    private Validator() {}

    /** Allow-lists to digits only, so the result cannot alter SQL structure. */
    public static String sanitizeId(String raw) {
        String s = StkGeneral.nullCheck(raw);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c >= '0' && c <= '9') {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    public static int toInt(String raw) {
        try {
            return Integer.parseInt(StkGeneral.nullCheck(raw));
        } catch (NumberFormatException e) {
            return -1;
        }
    }
}
