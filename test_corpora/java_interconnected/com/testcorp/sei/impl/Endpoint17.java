package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl5;
import com.testcorp.manager.Manager7;
import com.testcorp.facade.Facade1;
import com.testcorp.service.Service17;

@WebService
public class Endpoint17 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service17().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl5();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager7().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager7().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade1().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade1().orchestrateDirect(id);
    }
}
