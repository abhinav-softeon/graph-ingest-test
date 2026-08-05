package com.testcorp.manager;

import com.testcorp.service.Service3;
import com.testcorp.util.StkGeneral;

public class Manager3 {
    private final Service3 svc = new Service3();

    public String process(String id) throws Exception {
        return svc.handle(StkGeneral.nullCheck(id));
    }

    /** Routes through the service's peer, adding two frames to the chain. */
    public String deep(String id) throws Exception {
        return svc.viaPeer(id);
    }

    /** Hands off to a PEER MANAGER, which then goes deep. This is what pushes the
      * longest chain past six frames: facade -> manager -> peer manager -> service
      * -> peer service -> dao. Anything with a small hop bound loses it entirely. */
    public String chain(String id) throws Exception {
        return new Manager4().deep(id);
    }
}
