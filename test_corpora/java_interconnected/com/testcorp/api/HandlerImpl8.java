package com.testcorp.api;

import com.testcorp.service.Service8;

public class HandlerImpl8 implements Handler {
    private final Service8 svc = new Service8();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
