package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl1;
import com.testcorp.manager.Manager3;
import com.testcorp.facade.Facade5;
import com.testcorp.service.Service13;

@WebService
public class Endpoint13 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service13().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl1();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager3().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager3().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade5().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade5().orchestrateDirect(id);
    }
}
