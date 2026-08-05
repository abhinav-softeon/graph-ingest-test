package com.testcorp.api;

import com.testcorp.service.Service7;

public class HandlerImpl7 implements Handler {
    private final Service7 svc = new Service7();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
